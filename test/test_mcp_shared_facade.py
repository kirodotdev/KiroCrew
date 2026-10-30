"""``kiro_crew.mcp_shared`` keeps the patch seams and the surface the one module had.

``kiro_crew.mcp_shared`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners
in its ``_OWNERS``, lowest layer first: ``mcp_shared_wire``, ``mcp_shared_calls``,
``mcp_shared_policy`` and ``mcp_shared_dispatch``. It keeps the server's entry,
``run_mcp_stdio_loop``, with the process identity it declares and its hard-exit signal
handlers, and every name an owner defines still resolves as ``kiro_crew.mcp_shared.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it; the owner list and its layering; that each global rebound at run
time lives in the one module that rebinds it, with every reader beside it; that every public
name the module held before the split still type-checks through it; that the family logs
through one ``kiro_crew.mcp_shared`` logger; that ``ToolCancelled`` keeps the module name its
tracebacks show; that the managed-tools test still measures from the package directory; and
the frozen import inventory that stands in for the F401 check ``setup.cfg`` switches off for
it.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

import kiro_crew
from kiro_crew import _facade_mirror, mcp_shared

pytestmark = pytest.mark.xdist_group(name="tree_scan_mcp_shared_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
    "owner_layering",
    "home_manifest",
)

FACADE = "kiro_crew.mcp_shared"
OWNERS = (
    "kiro_crew.mcp_shared_wire",
    "kiro_crew.mcp_shared_calls",
    "kiro_crew.mcp_shared_policy",
    "kiro_crew.mcp_shared_dispatch",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: a wire helper, a tool-call bound, a policy cache and its failure state, the
#: dispatch loop the entry calls, a name the facade only imports, and a global rebound at
#: run time. The scan below must find all of them, so a scanner that stops matching goes red
#: instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "respond",
        "notify",
        "_read_message",
        "_framing",
        "_stdout_fd",
        "MAX_CONTENT_LENGTH_BYTES",
        "DEFERRED_STEP_WORKERS",
        "_StepPool",
        "_resolve_tool_policy",
        "_excluded_tools_by_session",
        "_failure_count",
        "_run_stdio_dispatch_loop",
        "_internal_caller_name",
        "loopback_urlopen",
        "sel",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``mcp_shared_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.mcp_shared(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bmcp_shared\b(?!_)|[^\n]*?\bmcp_shared\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    Any Callable CallerContext ID_PROBE_BYTES INFLIGHT_CAPABILITY_KEY INFLIGHT_NOTIFICATION
    INSTALL_PRUNED_EXIT_CODE JSONRPC_INTERNAL_ERROR JSONRPC_INVALID_PARAMS
    JSONRPC_INVALID_REQUEST JSONRPC_PARSE_ERROR JsonRpcEnvelopeError KiroCrewConfig Literal
    Mapping NamedTuple Optional STALE_MANAGED_MCP_SERVERS ValidationError build_tool_response
    caller_identity_capability collections concurrent config_dir contextlib current_caller
    functools install_pruned json logging loopback_urlopen neutralize_markers os overload
    parse_dashboard_url parse_json_object_line platform_compat queue read_local_secret
    recover_line_id recover_top_level_id resolve_client_port_src resolve_own_identity
    respawned_by_pool sel select session_key_from_env_token set_current_caller
    set_current_tenant_nonce signal sys tenant_nonce_from_meta threading time types urllib
    validate_jsonrpc_request validate_jsonrpc_response
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib"})

#: Each global the family rebinds at run time (``global name``), with the one module that
#: does, and so holds it; every module that reads the name sits beside it.
_REBOUND_HOMES = {
    "_internal_caller_name": FACADE,
    "_stdout_fd": "kiro_crew.mcp_shared_wire",
    "_framing": "kiro_crew.mcp_shared_wire",
    "_last_failure_time": "kiro_crew.mcp_shared_policy",
    "_last_startup_race_time": "kiro_crew.mcp_shared_policy",
    "_last_startup_race_key": "kiro_crew.mcp_shared_policy",
    "_failure_count": "kiro_crew.mcp_shared_policy",
    "_last_failure_class": "kiro_crew.mcp_shared_policy",
}


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(mcp_shared)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [mcp_shared, *(sys.modules[name] for name in _index().owners)]


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
        "from kiro_crew import mcp_shared\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(mcp_shared, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.mcp_shared as mod\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(mod, 'B'):\n        pass\n",
        {"B"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.mcp_shared.C', 1)\n",
        {"C"},
    ),
    "attribute-assignment": (
        "from kiro_crew import mcp_shared\ndef t():\n    mcp_shared.D = 1\n",
        {"D"},
    ),
    # An owner is not the facade, even bound under the facade's name.
    "owner-named-like-the-facade": (
        "from kiro_crew import mcp_shared_wire as mcp_shared\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(mcp_shared, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.mcp_shared_policy.Y'):\n        pass\n",
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
        if not hasattr(mcp_shared, name) or isinstance(getattr(mcp_shared, name), ModuleType):
            continue  # a shared module (os, sys, select) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mcp_shared, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(mcp_shared, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# The owners, their layering and the globals they rebind
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == mcp_shared._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == mcp_shared._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_mcp_shared_sibling() -> None:
    assert mcp_shared.__file__ is not None
    siblings = sorted(
        f"kiro_crew.{path.stem}"
        for path in Path(mcp_shared.__file__).parent.glob("mcp_shared_*.py")
    )
    assert siblings == sorted(OWNERS)


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
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - set(OWNERS[:position]))
        if bad:
            gaps[owner] = bad
    return gaps


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.mcp_shared_dispatch import _WakePipe\n")
    trees[OWNERS[2]] = ast.parse("from kiro_crew import mcp_shared\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[3]],
        OWNERS[2]: [FACADE],
    }


def _rebound_globals(tree: ast.Module) -> set[str]:
    return {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}


def _global_reads(module: ModuleType) -> set[str]:
    return facade_mirror_scan.global_reads(_source(module), module.__name__)


def test_each_rebound_global_lives_with_its_writer_and_every_reader() -> None:
    """A global rebound with ``global name`` exists in one namespace, the writer's: a
    reader in another module would keep the value bound at import. So the writer holds
    it alone, no other member binds it, and every member that reads it is the writer."""
    family = _family()
    rebound = {m.__name__: _rebound_globals(_tree(m)) for m in family}
    assert {n: home for home, names in rebound.items() for n in names} == _REBOUND_HOMES
    for name, home in _REBOUND_HOMES.items():
        holders = [m.__name__ for m in family if name in vars(m)]
        readers = [m.__name__ for m in family if name in _global_reads(m)]
        assert holders == [home], name
        assert readers == [home], name


def test_a_facade_write_of_a_rebound_global_lands_with_its_writer() -> None:
    wire = sys.modules["kiro_crew.mcp_shared_wire"]
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_shared, "_framing", sentinel)
        assert vars(wire)["_framing"] is sentinel
        assert mcp_shared._framing is sentinel
        assert "_framing" not in vars(mcp_shared)
    assert vars(wire)["_framing"] is not sentinel
    assert "_framing" not in vars(mcp_shared)
    assert _index().divergent() == ()


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
    for node in _tree(mcp_shared).body:
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
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(mcp_shared))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """The stdio servers import ``call_tool_with_logging``, ``DeferredTool`` and the rest
    from here, so mypy must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(mcp_shared))) == []


# --------------------------------------------------------------------------- #
# Seams particular to this facade
# --------------------------------------------------------------------------- #


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """Captures and filters name ``kiro_crew.mcp_shared`` (``caplog.set_level(...,
    logger=...)``, ``record.name ==``), and a handler added to ``mcp_shared.logger`` must
    see the dispatch loop's records: every member binds that one logger object."""
    loggers = [vars(m)["logger"] for m in _family() if "logger" in vars(m)]
    assert len(loggers) >= 4, "fewer loggers than members that log -- the scan went blind"
    assert {lg.name for lg in loggers} == {FACADE}
    assert all(lg is mcp_shared.logger for lg in loggers)


def test_tool_cancelled_keeps_the_module_name_its_tracebacks_show() -> None:
    from kiro_crew.mcp_shared_calls import ToolCancelled

    assert ToolCancelled is mcp_shared.ToolCancelled
    assert ToolCancelled.__module__ == FACADE
    assert f"{ToolCancelled.__module__}.{ToolCancelled.__qualname__}" == (
        "kiro_crew.mcp_shared.ToolCancelled"
    )


def test_the_managed_tools_test_still_measures_from_the_package_directory() -> None:
    """``serves_managed_tools`` asks whether a handler is defined under the ``kiro_crew``
    package, from ``_PACKAGE_DIR``: an owner in a subpackage would measure from there and
    exempt every core server from the managed tool policy."""
    package_dir = os.path.dirname(os.path.realpath(kiro_crew.__file__))
    assert mcp_shared._PACKAGE_DIR == package_dir
    assert mcp_shared.serves_managed_tools(mcp_shared.respond)


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, and from each owner module only
    names that owner defines. Imports under ``if TYPE_CHECKING:`` bind nothing at run
    time and are the mirror guard's business, so they are skipped.
    """
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
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in OWNERS}


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    assert unexpected_imports(_source(mcp_shared), _owner_names()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    planted = (
        _source(mcp_shared)
        + "\nimport pickle\nfrom kiro_crew.mcp_shared_wire import _run_stdio_dispatch_loop\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "pickle",
        "kiro_crew.mcp_shared_wire._run_stdio_dispatch_loop",
    ]
