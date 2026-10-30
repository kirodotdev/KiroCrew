"""``kiro_crew.session_pid`` keeps the patch seams and the surface the one module had.

``kiro_crew.session_pid`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``session_pid_*.py`` owners beside it. Callers, tests and documents still reach every
name as ``kiro_crew.session_pid.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it, by any spelling they use, reaches every module holding it; the owners are exactly the
``session_pid_*.py`` siblings, each importing only the owners before it; the family logs
through one logger named for the facade; and every project name the facade imported
before its owners moved out stays bound on it, so a reader still type-checks through the
facade path.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, session_pid

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_pid_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.session_pid"
SRC_DIR = Path(session_pid.__file__).resolve().parent

#: ``session_pid_*.py`` siblings that are modules of their own, not owners: the HMAC
#: sidecar contract for ``session_pid_<pid>`` mappings, which never lived in the facade.
_NOT_OWNERS = frozenset({"kiro_crew.session_pid_sig"})

#: Names the test trees are known to patch through the facade, one per kind of seam: a
#: tracking-file probe every sweep reads, an environ marker read several owners call, the
#: protected-pid registry's state, a flag an owner rebinds through ``global``, a
#: constant computed at import, a function the facade keeps, a project import and a
#: stdlib module. The scan must find all of them, so a scanner that stops matching goes
#: red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_pid_start_token",
        "_pid_file_path",
        "_env_has_kirocrew_marker",
        "_our_orphan_pids",
        "_PROTECTED_PIDS",
        "_rss_ceiling_inert_warned",
        "_GATEWAYD_TERM_GRACE_SECONDS",
        "_kill_pid_tree",
        "config_dir",
        "sys",
    }
)

#: The project names the facade imported before its owners moved out, with their home
#: modules. A reader that spells one through ``kiro_crew.session_pid`` keeps
#: type-checking only while the facade binds it itself.
_BASE_PROJECT_IMPORTS = {
    "platform_compat": "kiro_crew",
    "agent_process_markers": "kiro_crew.agent_sdk.backends",
    "node_adapter_entry_relpaths": "kiro_crew.agent_sdk.backends",
    "atomic_write": "kiro_crew.atomic_write",
    "config_dir": "kiro_crew.config.paths",
    "KIROCREW_SANDBOX_TOOL_ENV": "kiro_crew.constants",
    "KIROCREW_SANDBOX_TOOL_VALUE": "kiro_crew.constants",
    "KIROCREW_SPAWN_HOME_ENV": "kiro_crew.constants",
    "KIROCREW_SPAWN_INSTANCE_ENV": "kiro_crew.constants",
    "KIROCREW_SPAWNED_ENV": "kiro_crew.constants",
    "KIROCREW_SPAWNED_VALUE": "kiro_crew.constants",
    "TOTAL_SHUTDOWN_BUDGET_SECS": "kiro_crew.mcp_gateway.shutdown_budget",
    "ensure_directory": "kiro_crew.owner_only_files",
    "owner_only_opener_for": "kiro_crew.owner_only_files",
    "PidRefcount": "kiro_crew.runtime_ownership",
    "authorize_runtime_kill": "kiro_crew.runtime_ownership",
    "commit_runtime_teardown": "kiro_crew.runtime_ownership",
    "release_runtime_teardown": "kiro_crew.runtime_ownership",
    "tenancy_epoch": "kiro_crew.runtime_ownership",
}


def _index() -> _facade_mirror.MirrorIndex:
    return session_pid.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [session_pid, *(sys.modules[name] for name in _index().owners)]


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


def _spellings() -> dict[str, str]:
    """The facade's own name and every src module's module-level binding of it
    (``cli_doctor``, ``diag.procs`` and ``runtime_reconcile`` bind
    ``from kiro_crew import session_pid``)."""
    spellings = {FACADE: FACADE}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=("session_pid",)):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                for alias in node.names:
                    if alias.name == "session_pid" and module != FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def _corpus_patched_names() -> set[str]:
    spellings = _spellings()
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
    """The reach the one module had: a write to ``session_pid.<name>`` was what every
    reader of the name read. Every member of the family that binds a patched name sees
    the patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(session_pid, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session_pid, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(session_pid, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_the_owners_are_the_session_pid_siblings_in_layer_order() -> None:
    """``_OWNERS`` names every ``session_pid_*.py`` owner beside the facade, and each owner
    imports only owners listed before it: the order the facade loads them and a read
    resolves in."""
    owners = list(_index().owners)
    on_disk = {f"kiro_crew.{p.stem}": p for p in SRC_DIR.glob("session_pid_*.py")}
    assert set(on_disk) >= _NOT_OWNERS, "a listed non-owner module is gone; drop it here"
    assert sorted(owners) == sorted(set(on_disk) - _NOT_OWNERS)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(on_disk[owner].read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``session_pid.py`` was a plain module: the facade forwards no legacy table and
    keeps the default ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.session_pid`` keep seeing the moved code."""
    assert session_pid.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert len(loggers) == len(_family())
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not session_pid.logger] == []


def _static_bindings(path: Path) -> set[str]:
    """Names a module binds at import time with a module-level ``import``, outside
    ``if TYPE_CHECKING:`` -- the bindings a type checker reads off the module."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return found


def _home(value: object) -> str:
    if isinstance(value, ModuleType):
        return value.__name__
    return getattr(value, "__module__", "") or ""


def test_every_project_name_the_facade_imported_stays_bound_on_it() -> None:
    """A name the facade imported from the rest of ``kiro_crew`` is bound on it still,
    statically and as its home module's object; and no name the mirror serves comes
    from a project module outside the family, which would be visible to a type checker
    only through a ``TYPE_CHECKING`` import."""
    bound = _static_bindings(Path(session_pid.__file__))
    assert "tenancy_epoch" in bound, "the static reader went blind"
    missing = sorted(name for name in _BASE_PROJECT_IMPORTS if name not in bound)
    assert missing == []
    for name, home in _BASE_PROJECT_IMPORTS.items():
        assert vars(session_pid)[name] is getattr(importlib.import_module(home), name), name
    family = {FACADE, *_index().owners}
    defined = {
        name
        for owner in _index().owners
        for name in _facade_mirror._defined_names(
            ast.parse(Path(sys.modules[owner].__file__ or "").read_text(encoding="utf-8"))
        )
    }
    served_from_outside = {
        name: _home(getattr(session_pid, name))
        for name in sorted(set(_index().exports) - defined)
        if _home(getattr(session_pid, name)).startswith("kiro_crew")
        and _home(getattr(session_pid, name)) not in family
    }
    assert served_from_outside == {}
