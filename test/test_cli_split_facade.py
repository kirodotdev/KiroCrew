"""``kiro_crew.cli`` keeps the patch seams and the surface the one module had.

``kiro_crew.cli`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners in
its ``_OWNERS``: ``cli_logging``, ``cli_jail``, ``cli_gateway_boot`` and ``cli_knowledge``.
``main`` reads the moved helpers as plain module globals, and callers, tests and documents
reach every name as ``kiro_crew.cli.<name>``. The command line's arguments come from the
``cli_parser_*`` registrar modules, which are not owners: they build parsers and read no
name a test patches through the facade.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this one: every name the test trees patch through it
reaches every module holding it; the owner list, with no exclusion and no forwarded table;
the names an owner serves from outside the family stay visible to type checkers, and every
public name the module held before the split still type-checks through it; the owners log
as ``kiro_crew.cli``; a facade write of the log-queue slot lands in the module that rebinds
it; and the registrars read no patched name, each called exactly once by ``main`` before
``cli_help.hide_internal_commands``.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, cli, cli_parser_admin, cli_parser_agent, cli_parser_gateway

pytestmark = pytest.mark.xdist_group(name="tree_scan_cli_split_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
    "home_manifest",
)

FACADE = "kiro_crew.cli"
OWNERS = (
    "kiro_crew.cli_logging",
    "kiro_crew.cli_jail",
    "kiro_crew.cli_gateway_boot",
    "kiro_crew.cli_knowledge",
)
REGISTRARS: tuple[ModuleType, ...] = (cli_parser_gateway, cli_parser_agent, cli_parser_admin)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: a helper ``main`` reads (facade-bound), a helper only an owner reads, the
#: log-queue slot, a name the facade only imports, and a module reached through it. The
#: scan below must find all of them, so a scanner that stops matching goes red instead of
#: passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_setup_cli_logging",
        "_jail_reexec_gate",
        "_child_argv",
        "_fd_targets_file",
        "_redirect_fds_to",
        "_stop_log_queue_listener",
        "_LOG_QUEUE_LISTENER",
        "_ensure_node",
        "_node_ok",
        "boot_platform",
        "KiroCrewConfig",
        "SessionManager",
        "sel",
        "sys",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``cli_*`` sibling.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.cli(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bcli\b(?!_)|[^\n]*?\bcli\b(?!_))"
)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(cli)[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [cli, *(sys.modules[name] for name in _index().owners)]


def _tree(module: ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-from-import": (
        "from kiro_crew import cli\n" "def t(monkeypatch):\n    monkeypatch.setattr(cli, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.cli as kc_cli\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(kc_cli, 'B'):\n        pass\n",
        {"B"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.cli.C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.cli.D', 1)\n",
        {"D"},
    ),
    "sys-modules-assignment": (
        "import sys\n" "def t():\n    cli = sys.modules.get('kiro_crew.cli')\n    cli.E = 1\n",
        {"E"},
    ),
    "assignment-and-del": (
        "from kiro_crew import cli as m\ndef t():\n    m.F = 1\n    del m.G\n",
        {"F", "G"},
    ),
    # A sibling module is not the facade, even bound under a name that looks like it.
    "sibling-named-cli": (
        "from kiro_crew import cli_server as cli\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(cli, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.cli_logging.Y'):\n        pass\n",
        set(),
    ),
    "name-chosen-at-run-time": (
        "from kiro_crew import cli\n"
        "def t(monkeypatch, name):\n    monkeypatch.setattr(cli, name, 1)\n",
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
        if not hasattr(cli, name) or isinstance(getattr(cli, name), ModuleType):
            continue  # a shared module (os, sys, asyncio) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cli, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(cli, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_facade_installs_when_it_runs_as_main(tmp_path: Path) -> None:
    """``python -m kiro_crew.cli`` runs this file as ``__main__``. The mirror's one object
    per name must hold under that name too, or the install refuses the module and the
    run dies on import; the logger the owners share is bound by name for that reason."""
    result = subprocess.run(
        [sys.executable, "-m", "kiro_crew.cli", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "KIROCREW_HOME": str(tmp_path / "home")},
    )
    assert result.returncode == 0, result.stderr


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == cli._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == cli._EXPORTS
    assert index.divergent() == ()


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_no_owner_or_registrar_imports_the_facade_or_an_owner() -> None:
    """The facade imports the owners; they, and the registrars, import neither it nor
    each other, so the owners' order in ``_OWNERS`` is no import order at all."""
    split_out = [*(sys.modules[name] for name in OWNERS), *REGISTRARS]
    reached = {
        module.__name__: sorted(_project_imports(_tree(module)) & {FACADE, *OWNERS})
        for module in split_out
    }
    assert {name: found for name, found in reached.items() if found} == {}


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
    for node in _tree(cli).body:
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
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(cli))
    assert sorted(missing) == []


def test_the_static_visibility_scan_can_fail() -> None:
    tree = ast.parse("import os\nif TYPE_CHECKING:\n    from a import b\nX = 1\n")
    assert _statically_bound(tree) == {"os", "b", "X"}
    assert "c" not in _statically_bound(tree)


# --------------------------------------------------------------------------- #
# Seams particular to this facade
# --------------------------------------------------------------------------- #


def test_the_owners_log_under_the_facade_name() -> None:
    assert cli.logger.name == FACADE
    assert [
        m.__name__ for m in _family() if vars(m).get("logger", cli.logger) is not cli.logger
    ] == []


def test_a_facade_write_of_the_log_queue_slot_reaches_its_writers() -> None:
    """``_LOG_QUEUE_LISTENER`` is the slot ``_setup_cli_logging`` and
    ``_stop_log_queue_listener`` rebind with ``global``: it lives in the one module holding
    both, the facade never binds it, and a write through the facade lands there."""
    home = [m for m in _family() if "_LOG_QUEUE_LISTENER" in vars(m)]
    assert [m.__name__ for m in home] == ["kiro_crew.cli_logging"]
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "_LOG_QUEUE_LISTENER", sentinel)
        assert vars(home[0])["_LOG_QUEUE_LISTENER"] is sentinel
        assert cli._LOG_QUEUE_LISTENER is sentinel
    assert "_LOG_QUEUE_LISTENER" not in vars(cli)
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# The registrars
# --------------------------------------------------------------------------- #


def _registrar_reads(sources: dict[str, str], patched: frozenset[str]) -> dict[str, list[str]]:
    """Module -> the patched names its compiled code reads as globals."""
    out: dict[str, list[str]] = {}
    for name, source in sources.items():
        hit = sorted(facade_mirror_scan.global_reads(source, name) & patched)
        if hit:
            out[name] = hit
    return out


def test_the_registrars_read_no_name_the_tests_patch() -> None:
    """The registrars are not owners, so a patch through the facade does not reach them:
    that is sound only while they read none of the names the test trees patch."""
    sources = {}
    for module in REGISTRARS:
        assert module.__file__ is not None
        sources[module.__name__] = Path(module.__file__).read_text(encoding="utf-8")
    assert _registrar_reads(sources, _corpus_patched_names()) == {}


def test_the_registrar_read_scan_can_fail() -> None:
    probe = "def register_x_parser(sub):\n    return boot_platform(sub)\n"
    assert _registrar_reads({"probe": probe}, frozenset({"boot_platform"})) == {
        "probe": ["boot_platform"]
    }


def _main_registrar_calls() -> tuple[Counter[str], list[int], int]:
    main = next(
        node
        for node in _tree(cli).body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls: Counter[str] = Counter()
    lines: list[int] = []
    hide_line = -1
    for node in ast.walk(main):
        if not isinstance(node, ast.Call):
            continue
        dotted = ast.unparse(node.func)
        head, _, attr = dotted.rpartition(".")
        if head in {m.__name__.rpartition(".")[2] for m in REGISTRARS}:
            calls[f"{head}.{attr}"] += 1
            lines.append(node.lineno)
        elif dotted == "cli_help.hide_internal_commands":
            hide_line = node.lineno
    return calls, lines, hide_line


def test_main_calls_every_registrar_exactly_once_before_hiding_the_internal_commands() -> None:
    defined = Counter(
        f"{module.__name__.rpartition('.')[2]}.{node.name}"
        for module in REGISTRARS
        for node in _tree(module).body
        if isinstance(node, ast.FunctionDef) and re.fullmatch(r"register_\w+_parser", node.name)
    )
    calls, lines, hide_line = _main_registrar_calls()
    assert defined, "no registrar found -- the scan went blind"
    assert calls == defined
    assert hide_line > 0 and max(lines) < hide_line
