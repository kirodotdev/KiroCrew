"""``kiro_crew.acp.kas_agents`` keeps the patch seams and the surface the one module had.

``kiro_crew.acp.kas_agents`` keeps the agent projection and is a mirror facade
(:mod:`kiro_crew._facade_mirror`) over the ``kas_agents_*.py`` owners beside it: the
``mcpServers`` projection, the session-level hoist and the registered-entry reads.
Callers, tests and documents still reach every name as
``kiro_crew.acp.kas_agents.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it, a moved owner's read included; the owners are
exactly the ``kas_agents_*.py`` siblings, each importing only the owners before it; and
the family logs through one logger named for the facade.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.acp import kas_agents

pytestmark = pytest.mark.xdist_group(name="tree_scan_kas_agents_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams",)

FACADE = "kiro_crew.acp.kas_agents"
SRC_DIR = Path(kas_agents.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, one per kind of seam: a
#: read both the facade and a moved owner make (``_registry_governed``), a function the
#: facade keeps, a project import its own code reads, and a module-level memo. The scan
#: must find all of them, so a scanner that stops matching goes red instead of passing
#: over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_registry_governed",
        "build_kas_custom_agents",
        "get_global_hook_store",
        "_SPEC_SCAN_MEMO",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return kas_agents.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [kas_agents, *(sys.modules[name] for name in _index().owners)]


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            found.append(path)
    return found


def _spellings() -> dict[str, str]:
    """The facade's own name and every src module's module-level binding of it
    (``from kiro_crew.acp import kas_agents``)."""
    spellings = {FACADE: FACADE}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=("kas_agents",)):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.acp":
                for alias in node.names:
                    if alias.name == "kas_agents" and module != FACADE:
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
    """The reach the one module had: a write to ``kas_agents.<name>`` was what every
    reader of the name read. Every member of the family that binds a patched name sees
    the patched object, and the undo leaves the family one object per name again."""
    family = _family()
    missed: dict[str, list[str]] = {}
    owner_held: set[str] = set()
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(kas_agents, name):
            continue  # a test that creates the name with raising=False
        holders = [module for module in family if name in vars(module)]
        owner_held |= {name} if any(m is not kas_agents for m in holders) else set()
        sentinel = object()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(kas_agents, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(kas_agents, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    # ``_project_mcp_servers`` reads it in its owner: the reach test measured an owner.
    assert "_registry_governed" in owner_held
    assert _index().divergent() == ()


def test_the_owners_are_the_kas_agents_siblings_in_layer_order() -> None:
    """The owners are every ``kas_agents_*.py`` beside the facade, and each imports only
    owners listed before it and never the facade: the order the facade loads them."""
    owners = list(_index().owners)
    on_disk = {f"kiro_crew.acp.{p.stem}": p for p in SRC_DIR.glob("kas_agents_*.py")}
    assert sorted(owners) == sorted(on_disk)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(on_disk[owner].read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if FACADE in imported:
            later.append(FACADE)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_serves_only_the_moved_names_only_owner_code_reads() -> None:
    """A moved name a caller or the facade's own code reads is bound on the facade; the
    rest (``_SERVED``) is read from its owner, and no legacy table is forwarded."""
    index = _index()
    assert dict(index.forwards) == {}
    assert index.excluded == ()
    assert set(index.served) == set(kas_agents._SERVED)
    assert set(kas_agents._EXPORTS) == set(kas_agents._SERVED)
    for name, owner in index.served.items():
        assert name not in vars(kas_agents), name
        assert getattr(kas_agents, name) is vars(sys.modules[owner])[name], name


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.acp.kas_agents`` keep seeing the moved code."""
    assert kas_agents.logger.name == FACADE
    loggers = [m for m in _family() if "logger" in vars(m)]
    assert {m.__name__ for m in loggers} >= {
        FACADE,
        "kiro_crew.acp.kas_agents_mcp_servers",
        "kiro_crew.acp.kas_agents_session_hoist",
    }
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not kas_agents.logger] == []
