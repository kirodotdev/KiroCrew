"""``kiro_crew.portability`` keeps the patch seams and the surface the one module had.

``kiro_crew.portability`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners in
its ``_OWNERS``, lowest layer first: ``portability_archive``, ``portability_templates``, ``portability_export`` and ``portability_vetting``. It keeps the import itself:
:func:`apply_import_zip` and the settings and session merges it runs. Every name an owner defines
still resolves as ``kiro_crew.portability.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it, and the scan finds the seams it is known to have; the owner list and
its layering; that every public name the module held before the split still type-checks
through it; that the owners log as ``kiro_crew.portability``; that a write of the
settings-document cap reaches the export, the vetting and the facade's own readers;
and the frozen import inventory that stands in for the F401 check
``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, portability

pytestmark = pytest.mark.xdist_group(name="tree_scan_portability_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
    "owner_layering",
    "one_home_per_name",
    "home_manifest",
    "owners_log_under_facade_name",
    "logger_scan_can_fail",
)

FACADE = "kiro_crew.portability"
OWNERS = (
    "kiro_crew.portability_archive",
    "kiro_crew.portability_templates",
    "kiro_crew.portability_export",
    "kiro_crew.portability_vetting",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: a helper an owner reads, a constant two owners and the facade read, a helper
#: the facade's own import code reads, and names the facade only imports. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_mc_dir",
        "_backup_sqlite",
        "_add_from_fd",
        "_read_first_line_fd",
        "_installed_template_names",
        "MAX_BUNDLED_AGENT_TEMPLATES",
        "_MAX_SETTINGS_DOCUMENT_BYTES",
        "_MAX_IMPORT_MEMBERS",
        "_strip_host_local_store_state",
        "_copy_import_session_fd",
        "_rollback_import_session",
        "config_dir",
        "kiro_agents_dir",
        "is_sensitive_path",
        "_vet_shell_command",
        "parse_imported_settings",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``portability_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.portability(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bportability\b(?!_)|[^\n]*?\bportability\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    ARCHIVE_DIR_NAME ARCHIVE_SEGMENT_DELIMITER ATTACHMENTS_DIR_SUFFIX Any COMPONENTS Callable
    ChannelSettings ConfigReadError ConfigWriteRefused ConversationLog EXPORT_MANIFEST_VERSION
    ExportPlan INCOGNITO_MEMORY_MODES Iterable Iterator MAX_DOCUMENT_NESTING
    MEMORY_STORES_DIR_NAME NotificationCopyUnsupported OWNED_KIRO_AGENT_FILES Path PurePath
    PurePosixPath SESSIONS_DIR_NAME THREADS_DIR_NAME THREADS_SIDECAR_SUFFIX
    TRANSCRIPT_HEADER_MAX_BYTES ZipInventoryRejected _DB_SIDECAR_GLOBS _copy_notifications
    _copy_tree_no_overwrite _core_file_not_installed _do_replace _install_core_file_if_absent
    _log_cron_denial _merge_crons _merge_memory _merge_named_stores _merge_notifications
    _refuse_corrupt_source_databases _safe_key _staging_is_pinned _vet_shell_command
    atomic_write cast clear_list_agents_cache config_dir contextlib create_export_zip crew_teams
    datetime deque exceeds_nesting io is_host_local_store_state is_sensitive_path json
    kiro_agents_dir logging math memory_mode_from_header_line os parse_imported_settings
    parsed_agent_specs pinned_fs plan_export platform_compat re sanitize_agent_config_governance
    shutil sqlite3 stat stream_export_zip strip_utf8_bom tempfile time timezone ui_prefs
    update_config_locked vet_zip_inventory zipfile zlib
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(portability)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [portability, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-from-import": (
        "from kiro_crew import portability\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(portability, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.portability as facade\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(facade, 'B'):\n        pass\n",
        {"B"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.portability.C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.portability.D', 1)\n",
        {"D"},
    ),
    # An owner is not the facade, even bound under the facade's name.
    "owner-named-like-the-facade": (
        "from kiro_crew import portability_archive as portability\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(portability, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.portability_vetting.Y'):\n        pass\n",
        set(),
    ),
}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


def _reach_gaps(names: set[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """``owner.name`` for each patched name an owner binds that the index does not route."""
    return [
        f"{module.__name__}.{name}"
        for name in sorted(names)
        for module in _family()[1:]
        if name in vars(module) and module.__name__ not in index.holders.get(name, ())
    ]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(portability, name) or isinstance(getattr(portability, name), ModuleType):
            continue  # a shared module (os, time) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(portability, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(portability, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_a_facade_write_of_the_settings_document_cap_reaches_every_reader() -> None:
    """``_MAX_SETTINGS_DOCUMENT_BYTES`` is read by the export's manifest budget, the
    vetting's manifest read and the facade's own settings and session-mtime reads; the
    tests shrink it through the facade, so the write must land in all of them."""
    sentinel = object()
    readers = [m for m in _family() if "_MAX_SETTINGS_DOCUMENT_BYTES" in vars(m)]
    assert {m.__name__ for m in readers} >= {
        FACADE,
        "kiro_crew.portability_archive",
        "kiro_crew.portability_export",
        "kiro_crew.portability_vetting",
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(portability, "_MAX_SETTINGS_DOCUMENT_BYTES", sentinel)
        assert [
            m.__name__ for m in readers if vars(m)["_MAX_SETTINGS_DOCUMENT_BYTES"] is not sentinel
        ] == []
    assert all(vars(m)["_MAX_SETTINGS_DOCUMENT_BYTES"] is not sentinel for m in readers)


# --------------------------------------------------------------------------- #
# The owners and their layering
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == portability._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == portability._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_portability_sibling() -> None:
    assert portability.__file__ is not None
    siblings = sorted(
        f"kiro_crew.{path.stem}"
        for path in Path(portability.__file__).parent.glob("portability_*.py")
    )
    # ``portability_stream`` is a sibling but not an owner: it builds the export archive,
    # reads every portability name as ``portability.<name>`` at call time and is
    # re-exported by the facade at its foot, so it uses the facade instead of holding
    # a part of it.
    assert siblings == sorted([*OWNERS, "kiro_crew.portability_stream"])


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _layering_gaps(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that it may not: the facade, or an owner
    listed at or after it."""
    gaps: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - allowed)
        if bad:
            gaps[owner] = bad
    return gaps


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.portability_vetting import x\n")
    trees[OWNERS[1]] = ast.parse("import kiro_crew.portability\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[-1]],
        OWNERS[1]: [FACADE],
    }


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name in OWNERS for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """A module outside the family that imported an owner would hold bindings the
    facade's mirror never indexes, so a patch through the facade would miss it."""
    root = Path(portability.__file__).resolve().parent
    family = {f"{name.rpartition('.')[2]}.py" for name in (FACADE, *OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_all=("portability_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(root)), root
    )
    assert importers & (family - {"portability.py"})  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    owner = OWNERS[0].rpartition(".")[2]
    sources = {
        root / "pkg" / "caller.py": f"from kiro_crew.{owner} import x\n",
        root / "other.py": f"from kiro_crew import {owner} as low\n",
        root / "plain.py": f"import kiro_crew.{owner}\n",
        root / "relative.py": f"from .{owner} import x\n",
        root / "deep" / "up.py": f"from .. import {owner}\n",
        root / "facade_user.py": "from kiro_crew.portability import x\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "plain.py",
        "relative.py",
        "deep/up.py",
    }


def test_the_facade_binds_every_public_name_its_owners_define() -> None:
    """A reader of ``vars(facade)`` sees every public name the unsplit module held; only
    private owner names are served without a binding."""
    public = {
        name
        for owner in OWNERS
        for name in _facade_mirror._defined_names(_tree(sys.modules[owner]))
        if not name.startswith("_")
    }
    assert public, "the owners define no public name -- the scan went blind"
    assert sorted(public - set(vars(portability))) == []
    assert not any(name in _index().exports for name in public)


# --------------------------------------------------------------------------- #
# Static visibility
# --------------------------------------------------------------------------- #


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family."""
    family = set(OWNERS)
    found: dict[str, tuple[str, str]] = {}
    for owner in OWNERS:
        for node in _tree(sys.modules[owner]).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    out: dict[str, tuple[str, str]] = {}
    for node in _tree(portability).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(portability))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """Callers import the moved names from here, so a type checker must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(portability))) == []


# --------------------------------------------------------------------------- #
# The logger
# --------------------------------------------------------------------------- #


def _logger_names(tree: ast.Module) -> list[str]:
    """The argument of every ``logging.getLogger(...)`` call, unparsed."""
    return [
        ast.unparse(node.args[0]) if node.args else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "logging.getLogger"
    ]


# --------------------------------------------------------------------------- #
# The import inventory
# --------------------------------------------------------------------------- #


def _read_names(source: str) -> set[str]:
    """Every name *source*'s code reads: the loads of a bare name, annotations included."""
    return {
        node.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, from each owner module only names
    that owner defines, and any import the family's code reads, so a later change that
    imports a name for code it adds is not refused. Imports under ``if TYPE_CHECKING:``
    bind nothing at run time and are the mirror guard's business, so they are skipped.
    """
    reads = _read_names(source) | {
        name for owner in OWNERS for name in _read_names(_source(sys.modules[owner]))
    }
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | reads:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in OWNERS}


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    assert unexpected_imports(_source(portability), _owner_names()) == []


def test_every_base_import_is_still_bound_on_the_facade() -> None:
    """The header is the BASE header: each name it bound is still in the namespace."""
    assert sorted(_BASE_IMPORTS - set(vars(portability))) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    planted = (
        _source(portability)
        + "\nimport pickle\nfrom kiro_crew.portability_archive import apply_import_zip\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "pickle",
        "kiro_crew.portability_archive.apply_import_zip",
    ]


def test_the_inventory_check_accepts_a_planted_import_the_code_reads() -> None:
    """A new import that code in the module reads is accounted for, not refused."""
    planted = _source(portability) + "\nimport pickle\n_PROTOCOL = pickle.HIGHEST_PROTOCOL\n"
    assert unexpected_imports(planted, _owner_names()) == []
