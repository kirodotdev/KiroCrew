"""``kiro_crew.security_posture`` stays the one import path and patch surface of its owners.

The posture module is a :mod:`kiro_crew._facade_mirror` facade: it keeps
``NON_EGRESS_REDACTION_MODULES``, and the rest lives in the ``security_posture_<role>.py``
owners beside it. ``_REDACTION_SINKS`` is joined, in registry order, from the two sink
modules (``security_posture_sinks_gateway`` then ``security_posture_sinks_delivery``) in
``security_posture_controls``, which builds the controls and the snapshot. Callers, tests and
documents still reach every name as ``kiro_crew.security_posture.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade:

* every name the test trees patch through it reaches every module holding it;
* the owners are exactly the ``security_posture_*.py`` siblings, loaded lowest layer first;
* every module-level name the unsplit file bound still resolves through it;
* the registry is the two sink parts joined in order, and each part is a non-empty tuple of
  rows;
* every name it serves from outside the family is named for type checkers;
* nothing outside the family imports an owner;
* the family logs under the facade's name, and the facade still runs as ``python -m``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew import security_posture as facade
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Walks the test trees and src/kiro_crew; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_security_posture_facade")

FACADE = "kiro_crew.security_posture"
OWNER_PREFIX = "kiro_crew.security_posture_"
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: A name the test trees are known to patch through the facade. The scan below must find
#: it, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset({"_CONTROLS"})

#: Every module-level name ``security_posture.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    Callable NON_EGRESS_REDACTION_MODULES PostureControl PostureItem _AUDIT_SURFACE_DETAIL
    _CONTROLS _CREDENTIAL_FAMILIES _EXFIL_HEURISTICS _REDACTION_SINKS _SCHEMA_REGISTRY_NAMES
    _audit_surface_items _control_payload _credential_family_items _denied_command_items
    _exfil_heuristic_items _own_namespace_prefixes _redaction_sink_items _sel_mod
    _sensitive_path_items _suspicious_pattern_items _token_auth_items _tool_schema_items
    _validation _write_protected_items asyncio build_posture_snapshot
    build_posture_snapshot_async dataclass governance_executor logger logging posture_counts
    posture_counts_async security
    """.split())


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('security_posture_'):]}": path
        for path in sorted(_SRC.glob("security_posture_*.py"))
    }


def _family() -> list[ModuleType]:
    return [facade, *(sys.modules[name] for name in _index().owners)]


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
        if "security_posture" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """A write to ``security_posture.<name>`` reaches every module of the family that binds
    the name, and the undo restores all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(facade, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        originals = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(facade, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(facade, name) is not sentinel:
                stale.append(FACADE)
        restored = [m.__name__ for m in holders if vars(m).get(name) is not originals[m.__name__]]
        if stale or restored:
            missed[name] = stale + [f"{module} not restored" for module in restored]
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_posture_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``security_posture_*.py`` sibling, and each owner imports only
    owners listed before it."""
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
    # Non-vacuity: the layering is real, an owner reads names from an owner below it.
    assert any(
        f"from {owners[0]} import" in _owner_paths()[owner].read_text(encoding="utf-8")
        for owner in owners[1:]
    )


def test_the_facade_keeps_the_default_read_path_and_the_non_egress_set() -> None:
    """``security_posture.py`` was a plain module: the facade forwards no legacy table and
    keeps the default ``forward_reads``, and it still defines the non-egress allowlist."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert "NON_EGRESS_REDACTION_MODULES" in vars(facade)
    assert "NON_EGRESS_REDACTION_MODULES" not in index.exports


def test_every_base_module_name_resolves_through_the_facade() -> None:
    """Each name the unsplit module bound is still an attribute of the facade, and a name an
    owner defines resolves to that owner's own object."""
    assert sorted(name for name in _BASE_MODULE_NAMES if not hasattr(facade, name)) == []
    for name, owner in _index().exports.items():
        assert getattr(facade, name) is vars(sys.modules[owner])[name], name


def test_the_registry_is_the_two_sink_parts_in_order() -> None:
    """The panel lists the sinks in registry order: the gateway part, then the delivery part,
    each a non-empty run of (label, module, detail) rows, with no row in both."""
    gateway = importlib.import_module(f"{OWNER_PREFIX}sinks_gateway")
    delivery = importlib.import_module(f"{OWNER_PREFIX}sinks_delivery")
    first, second = gateway._GATEWAY_SINKS, delivery._DELIVERY_SINKS
    assert first and second
    assert facade._REDACTION_SINKS == (*first, *second)
    for row in (*first, *second):
        assert isinstance(row, tuple) and len(row) == 3 and all(isinstance(x, str) for x in row)
    assert not set(first) & set(second)


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("kiro_crew") or node.module in family:
                continue
            for alias in node.names:
                if f"{node.module}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest(source: str) -> dict[str, tuple[str, str]]:
    """The ``TYPE_CHECKING`` from-imports of *source* from outside the family."""
    family = set(_index().owners)
    out: dict[str, tuple[str, str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def _facade_source() -> str:
    return Path(facade.__file__).read_text(encoding="utf-8")


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.security_posture import
    security`` type-checked. Where an owner holds one now, the facade names it from its home
    module under ``TYPE_CHECKING``, and nothing else."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest(_facade_source()) == expected


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    """The census above can fail: a manifest missing one served name differs."""
    source = _facade_source()
    dropped = source.replace("    from kiro_crew import sel as _sel_mod  # noqa: F401\n", "")
    assert dropped != source
    assert "_sel_mod" not in _home_manifest(dropped)
    assert "_sel_mod" in _home_manifest(source)


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
                name.startswith(OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers reach the owners' names through the facade, so a patch of it reaches them."""
    family = {f"{name.rpartition('.')[2]}.py" for name in (FACADE, *_index().owners)}
    sources = source_corpus.iter_candidate_sources(require_all=("security_posture_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "security_posture_controls.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.security_posture_controls import _CONTROLS\n",
        root / "other.py": "from kiro_crew import security_posture_sinks_gateway as rows\n",
        root / "relative.py": "from .security_posture_sinks_delivery import _DELIVERY_SINKS\n",
        root / "deep" / "up.py": "from .. import security_posture_controls\n",
        root / "facade_user.py": "from kiro_crew.security_posture import posture_counts\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "relative.py",
        "deep/up.py",
    }


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module."""
    assert facade.logger.name == FACADE
    others = [
        m.__name__ for m in _family() if vars(m).get("logger", facade.logger) is not facade.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.security_posture`` imported cleanly before the split; the
    mirror installs on ``__main__`` too."""
    env = dict(os.environ)
    src = str(_SRC.parent)
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
    assert source_corpus.repo_root() / "test" / "test_security_posture.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
