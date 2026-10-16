"""Issue Radar's five split modules keep the patch seams each had as one module.

``store``, ``github_client``, ``crew_runtime``, ``azure_client`` and ``crew_store`` under
``kiro_crew.apps.builtins.issue_radar.backend`` are mirror facades
(:mod:`kiro_crew._facade_mirror`) over the ``<stem>_<role>`` owners beside them. Callers and
tests still reach every name as ``<stem>.<name>``, and the moved code reads its names as
plain module globals, so a patch through a facade reaches that code only because the
mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules: one
object per name, the manifest, owners never importing their facade, ``create=True``
patches, and the reach of every name the test trees patch through it. These tests pin what
is particular to these five, one row each: the patch scan sees the seams each is known to
have, its owners are exactly its ``<stem>_*.py`` siblings loaded lowest layer first, every
name it serves from outside its family stays visible to type checkers, and its family logs
under the logger name the module had.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.issue_radar.backend import (
    azure_client,
    crew_runtime,
    crew_store,
    github_client,
    store,
)

pytestmark = pytest.mark.xdist_group(name="tree_scan_issue_radar_facades")

PKG = "kiro_crew.apps.builtins.issue_radar.backend"
_THIS_FILE = Path(__file__).resolve()


@dataclasses.dataclass(frozen=True)
class Facade:
    """One split module: the facade, the names tests are known to patch through it, the
    name its family logs under, and the exception classes it defines."""

    module: ModuleType
    #: Seams the scan must find, so a scan that stops matching goes red instead of letting
    #: the shared reach check pass over nothing.
    known_seams: frozenset[str]
    #: ``None`` for a module that bound no logger before the split.
    logger: str | None
    exceptions: tuple[str, ...] = ()

    @property
    def stem(self) -> str:
        return self.module.__name__.rpartition(".")[2]

    @property
    def index(self) -> _facade_mirror.MirrorIndex:
        return vars(self.module)[_facade_mirror.INDEX_ATTRIBUTE]

    def owner_paths(self) -> dict[str, Path]:
        directory = Path(self.module.__file__ or "").resolve().parent
        return {f"{PKG}.{path.stem}": path for path in sorted(directory.glob(f"{self.stem}_*.py"))}


FACADES = (
    Facade(
        store,
        # a path helper every owner reads, an imported name, and each owner's writers
        frozenset(
            {
                "app_data_dir",
                "apply_label_change_to_caches",
                "apply_pr_state_change_to_caches",
                "is_repo_connected",
                "issue_write_lock",
                "merge_tagging_suggestions",
                "read_deps_cache",
                "read_issues_cache",
                "read_pr_detail_cache",
                "repo_data_dir",
                "write_deps_cache",
                "write_investigation",
            }
        ),
        f"{PKG}.store",
        ("SettingsConflict",),
    ),
    Facade(
        github_client,
        # the ``gh`` chokepoints every owner reads, an imported name, each owner's calls
        frozenset(
            {
                "_batch_dependency_graph",
                "_gh_run",
                "_run_gh_api",
                "_run_gh_write",
                "fetch_dependency_edges",
                "get_pr_detail",
                "merge_pull_request",
                "probe_open_list",
                "sanitize_cli_stderr",
                "search_pulls",
            }
        ),
        f"{PKG}.github_client",
    ),
    Facade(
        crew_runtime,
        # an imported service, a latch the watchdog rebinds, the chat runner, owner calls
        frozenset(
            {
                "_autonudge_instance",
                "_disabling",
                "_rehydrate",
                "_run_chat",
                "compose_turn_prompt_async",
                "ensure_crew_session",
                "is_app_enabled",
                "rehydrate_slot_from_history_async",
                "sync_trust",
                "wake_crew",
            }
        ),
        "kirocrew.app.issue-radar",
    ),
    Facade(
        azure_client,
        # the ``az`` chokepoints every owner reads, imported names, each owner's calls
        frozenset(
            {
                "_audit",
                "_az_bin",
                "_az_invoke",
                "_az_run",
                "_current_identity",
                "_default_organization",
                "_policy_evaluations",
                "_resolve_host",
                "add_pr_comment",
                "list_repo_labels",
                "minimal_env",
                "quote",
                "sanitize_cli_stderr",
                "sel",
            }
        ),
        None,
    ),
    Facade(
        crew_store,
        # the chain holds, the landed check, the carry, an imported name, owner writers
        frozenset(
            {
                "_chain_depth_bound",
                "_fold_checkpoint",
                "_hold_chain_no_follow",
                "_landed_since",
                "_legacy_carry_rows",
                "_recorded_unit_order_at",
                "_unit_order_path",
                "atomic_write",
                "commit_work_progress",
                "set_crew_model_held",
            }
        ),
        f"{PKG}.crew_store",
        (
            "CrewLedgerEntryTooLarge",
            "CrewLedgerNotRecorded",
            "CrewLedgerUnavailable",
            "CrewStoreError",
        ),
    ),
)
_BY_STEM = {facade.stem: facade for facade in FACADES}
each_facade = pytest.mark.parametrize("facade", FACADES, ids=[f.stem for f in FACADES])


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/``, and the builtin apps' own test packages."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel:
            found.append(path)
    return found


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> dict[str, frozenset[str]]:
    """Facade stem -> every name the test trees patch through it, from one scan of the tree."""
    spellings = {facade.module.__name__: facade.module.__name__ for facade in FACADES}
    found: dict[str, set[str]] = {facade.stem: set() for facade in FACADES}
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "issue_radar" not in text:
            continue
        for module, names in facade_mirror_scan.patched_names(text, spellings).items():
            found[module.rpartition(".")[2]] |= names
    return {stem: frozenset(names) for stem, names in found.items()}


@each_facade
def test_the_patch_scan_finds_every_known_seam(facade: Facade) -> None:
    """Non-vacuity: a scan that finds nothing would pass the shared reach check over nothing."""
    assert sorted(facade.known_seams - _corpus_patched_names()[facade.stem]) == []


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)


@each_facade
def test_the_owners_are_every_sibling_module_in_layer_order(facade: Facade) -> None:
    """``_OWNERS`` lists every ``<stem>_*.py`` sibling, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(facade.index.owners)
    paths = facade.owner_paths()
    assert sorted(owners) == sorted(paths)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(paths[owner].read_text(encoding="utf-8"))
        imported = {
            f"{PKG}.{node.module}" if node.level else node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def _home_imports(facade: Facade) -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family, with their home."""
    family = set(facade.index.owners)
    found: dict[str, tuple[str, str]] = {}
    for path in facade.owner_paths().values():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.module == "__future__":
                continue
            home = node.module or ""
            if node.level:
                home = PKG + (f".{node.module}" if node.module else "")
            if not home.startswith("kiro_crew") or home in family:
                continue
            for alias in node.names:
                if f"{home}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (home, alias.name))
    return found


def _home_manifest(facade: Facade) -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, name)."""
    family = set(facade.index.owners)
    tree = ast.parse(Path(facade.module.__file__ or "").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


@each_facade
def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers(
    facade: Facade,
) -> None:
    """Before the split the module bound these by import, so ``<stem>.<name>`` type-checked.
    Now an owner holds them, so the facade names each one from its home module under
    ``TYPE_CHECKING``, and exactly those."""
    served = set(facade.index.exports)
    defined: set[str] = set()
    for path in facade.owner_paths().values():
        defined |= _facade_mirror._defined_names(ast.parse(path.read_text(encoding="utf-8")))
    expected = {
        name: home
        for name, home in _home_imports(facade).items()
        if name in served and name not in defined
    }
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest(facade) == expected


@pytest.mark.parametrize(
    ("facade", "name"),
    [(facade, name) for facade in FACADES for name in facade.exceptions],
    ids=[f"{facade.stem}.{name}" for facade in FACADES for name in facade.exceptions],
)
def test_a_moved_exception_is_named_by_the_path_callers_import_it_from(
    facade: Facade, name: str
) -> None:
    """A traceback renders a raised class as ``module.qualname``, so each class these
    modules define still names its facade, not its owner."""
    cls = getattr(facade.module, name)
    assert cls.__module__ == facade.module.__name__
    assert cls.__qualname__ == name


@each_facade
def test_the_family_logs_under_the_name_the_module_had(facade: Facade) -> None:
    """One logger, named as before the split, held by every member of the family that logs."""
    family = [facade.module, *(importlib.import_module(owner) for owner in facade.index.owners)]
    loggers = [vars(member)["logger"] for member in family if "logger" in vars(member)]
    if facade.logger is None:
        assert loggers == [], "the split gave a module that logged nothing a logger"
        return
    assert loggers, "no member of the family binds a logger"
    assert {logger.name for logger in loggers} == {facade.logger}
    assert len({id(logger) for logger in loggers}) == 1
