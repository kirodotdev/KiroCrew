"""``kiro_crew.acp.liveness`` keeps the surface and the patch seams the one module had.

``liveness.py`` keeps the oracle (:class:`~kiro_crew.acp.liveness.LivenessOracle`) and the
offloaded-consult guard, and re-exports every name its sibling owners define:
``liveness_evidence``, ``liveness_proc``, ``liveness_command``, ``liveness_interactive``
and ``liveness_tool_state``. It is a plain facade, with no module ``__getattr__`` and no
mirror, so a patch through it rebinds the facade's own global. The oracle reads that
global, so such a patch reaches what it reached in the one module exactly when no owner
reads the patched name as a global of its own. These tests pin that from the patches the
test trees make, and pin the facade's surface, its owners' layering, that production code
imports the owners only through it, and its logger.
"""

from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew.acp import liveness

pytestmark = pytest.mark.xdist_group(name="tree_scan_liveness_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.acp.liveness"
#: The owners, lowest layer first: each imports only owners listed before it.
OWNERS = (
    "kiro_crew.acp.liveness_evidence",
    "kiro_crew.acp.liveness_proc",
    "kiro_crew.acp.liveness_command",
    "kiro_crew.acp.liveness_interactive",
    "kiro_crew.acp.liveness_tool_state",
)
#: Names the test trees are known to patch through the facade, both read by the oracle:
#: the boot-clock converter and the darwin backend selector. The scan must find them, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset({"process_start_boot_secs", "select_darwin_backend"})

_THIS_FILE = Path(__file__).resolve()


def _owner_modules() -> list[ModuleType]:
    return [importlib.import_module(name) for name in OWNERS]


def _owner_sources() -> dict[str, str]:
    return {
        module.__name__: Path(module.__file__ or "").read_text(encoding="utf-8")
        for module in _owner_modules()
    }


def _test_tree_files() -> Iterator[Path]:
    """Every file pytest collects tests or fixtures from: the root conftest, ``test/``,
    and the test modules and conftests under ``src/kiro_crew/apps/builtins``."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if rel == "conftest.py" or path.is_relative_to(test_root):
            yield path
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        ):
            yield path


def _patched_names(text: str) -> set[str]:
    """The names *text* patches through the facade, by every spelling the scanner reads."""
    spellings = {FACADE: FACADE}
    if not facade_mirror_scan._mentions(text, spellings):
        return set()
    names = facade_mirror_scan.text_patched_names(text, spellings).get(FACADE, set())
    try:
        events = facade_mirror_scan.patch_events(text, spellings)
    except SyntaxError:
        return set(names)
    return set(names) | facade_mirror_scan.names_by_target(events, {FACADE}).get(FACADE, set())


def _corpus_patched_names() -> set[str]:
    """Every name the test trees patch through the facade, less this file's planted ones."""
    names: set[str] = set()
    for path in _test_tree_files():
        if path.resolve() == _THIS_FILE:
            continue
        names |= _patched_names(path.read_text(encoding="utf-8", errors="replace"))
    return names


def _owner_reads(sources: dict[str, str], patched: set[str]) -> dict[str, list[str]]:
    """Owner -> the patched names its compiled code reads as a global, sorted.

    A name whose facade binding is a module is left out: a patch spelled through it
    (``liveness.sys.platform``) writes an attribute of that one shared module object, which
    every reader sees wherever it lives.
    """
    rebinding = {name for name in patched if not isinstance(vars(liveness).get(name), ModuleType)}
    found: dict[str, list[str]] = {}
    for owner, source in sources.items():
        hits = sorted(facade_mirror_scan.global_reads(source, owner) & rebinding)
        if hits:
            found[owner] = hits
    return found


def _top_level_definitions(source: str) -> set[str]:
    """The names *source* defines at its top level: each ``def``, ``class`` and assignment."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "conftest.py" in walked
    assert len(walked) > 1000


def test_the_scan_and_the_read_detector_catch_a_planted_seam() -> None:
    """Each patch spelling the corpus uses is read, and an owner reading a patched name as
    a global is reported, so the reach test below cannot pass over a shape it misses."""
    planted = (
        "from unittest.mock import patch\n"
        "from kiro_crew.acp import liveness\n"
        "import kiro_crew.acp.liveness as liveness_mod\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(liveness, "read_pid_stat", None)\n'
        '    monkeypatch.setattr("kiro_crew.acp.liveness.match_fragment", None)\n'
        '    with patch.object(liveness_mod, "first_program"):\n'
        "        pass\n"
    )
    assert _patched_names(planted) >= {"read_pid_stat", "match_fragment", "first_program"}
    owner = "def f(pid):\n    return read_pid_stat('/proc', pid)\n"
    reads = _owner_reads({"planted_owner": owner}, {"read_pid_stat", "sys"})
    assert reads == {"planted_owner": ["read_pid_stat"]}


def test_no_owner_reads_a_name_the_tests_patch_through_the_facade() -> None:
    """The reach the one module had: a write to ``liveness.<name>`` was what every reader
    of the name read. The facade's own code reads its globals, so each patched name must
    be read by facade code only."""
    patched = {name for name in _corpus_patched_names() if hasattr(liveness, name)}
    assert patched >= _KNOWN_SEAMS
    assert _owner_reads(_owner_sources(), patched) == {}


def test_a_module_valued_seam_is_the_one_module_object_in_every_holder() -> None:
    """``liveness.sys`` / ``liveness.time`` / ``liveness.asyncio`` are patched by attribute
    (``sys.platform``, ``time.clock_gettime``, ``asyncio.wait_for``): that reaches every
    owner only while each binds the very module object the facade binds."""
    for name in ("sys", "time", "asyncio", "platform_compat"):
        module = vars(liveness)[name]
        assert isinstance(module, ModuleType), name
        stale = [m.__name__ for m in _owner_modules() if vars(m).get(name, module) is not module]
        assert stale == [], name


def test_every_name_an_owner_defines_is_the_facades_name() -> None:
    """``from kiro_crew.acp.liveness import X`` keeps working for every moved ``X``,
    private helpers included, and hands out the owner's own object."""
    missing: dict[str, list[str]] = {}
    for module in _owner_modules():
        defined = _top_level_definitions(Path(module.__file__ or "").read_text(encoding="utf-8"))
        defined.discard("logger")
        assert defined, module.__name__
        wrong = sorted(n for n in defined if vars(liveness).get(n) is not vars(module)[n])
        if wrong:
            missing[module.__name__] = wrong
    assert missing == {}


def test_the_owners_import_downward_and_never_the_facade() -> None:
    """An owner never imports the facade, and imports only owners listed before it."""
    late: dict[str, list[str]] = {}
    for position, (owner, source) in enumerate(_owner_sources().items()):
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported |= {f"{node.module}.{alias.name}" for alias in node.names}
            elif isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
        assert FACADE not in imported, owner
        wrong = sorted(m for m in imported if m in OWNERS and OWNERS.index(m) >= position)
        if wrong:
            late[owner] = wrong
    assert late == {}
    assert sorted(OWNERS) == sorted(
        f"kiro_crew.acp.{p.stem}"
        for p in Path(liveness.__file__).resolve().parent.glob("liveness_*.py")
    )


def _owner_importers(sources: dict[str, str]) -> dict[str, list[str]]:
    """Module -> the owners it imports, for every module outside the family."""
    family = {FACADE, *OWNERS}
    found: dict[str, list[str]] = {}
    for module, source in sources.items():
        if module in family:
            continue
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
        owners = sorted(imported & set(OWNERS))
        if owners:
            found[module] = owners
    return found


def test_only_the_facade_imports_an_owner_in_production_code() -> None:
    """Callers import ``kiro_crew.acp.liveness``; an owner is the family's own business."""
    package = source_corpus.src_root()
    sources = {}
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "liveness" in text:
            parts = path.relative_to(package).with_suffix("").parts
            sources[".".join(("kiro_crew", *parts))] = text
    assert set(OWNERS) <= set(sources), "the scan no longer sees the owners themselves"
    assert _owner_importers(sources) == {}


def test_the_owner_import_scan_catches_each_spelling() -> None:
    planted = {
        "kiro_crew.planted_a": "from kiro_crew.acp.liveness_proc import read_pid_stat\n",
        "kiro_crew.planted_b": "from kiro_crew.acp import liveness_command\n",
        "kiro_crew.planted_c": "import kiro_crew.acp.liveness_tool_state\n",
        "kiro_crew.acp.liveness_interactive": "from kiro_crew.acp.liveness_command import x\n",
    }
    assert _owner_importers(planted) == {
        "kiro_crew.planted_a": ["kiro_crew.acp.liveness_proc"],
        "kiro_crew.planted_b": ["kiro_crew.acp.liveness_command"],
        "kiro_crew.planted_c": ["kiro_crew.acp.liveness_tool_state"],
    }


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.acp.liveness`` keep seeing the moved code."""
    assert liveness.logger.name == FACADE
    loggers = [m for m in _owner_modules() if "logger" in vars(m)]
    assert loggers, "an owner that logs is expected (the interactive classifier)"
    assert [m.__name__ for m in loggers if vars(m)["logger"] is not liveness.logger] == []


def test_the_facade_is_a_plain_module() -> None:
    """No ``__getattr__`` and no class swap: every name is a real global of the facade."""
    assert type(sys.modules[FACADE]) is ModuleType
    assert "__getattr__" not in vars(liveness)
