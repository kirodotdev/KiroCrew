"""``kiro_crew.tool_call_title`` keeps every name the one module had, as the same object.

The shell half of the tool-call title rules lives in two owners,
``tool_call_title_shell_parse`` and ``tool_call_title_shell_classify``, and
``kiro_crew.tool_call_title`` re-exports every name they define for importers: a plain
facade, because nothing patches the module. These tests pin that shape: the owners are
exactly the ``tool_call_title_shell_*.py`` siblings, each importing only the owner before
it and never the facade; every name an owner defines is bound on the facade, statically
(so a reader type-checks through the facade path) and as the owner's object; and no test
patches a name through the facade that an owner reads, which a plain re-export could not
reach -- the scan behind that answers both ways.
"""

from __future__ import annotations

import ast
import functools
import importlib
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import tool_call_title

pytestmark = pytest.mark.xdist_group(name="tree_scan_tool_call_title_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("corpus_gate",)

FACADE = "kiro_crew.tool_call_title"
SRC_DIR = Path(tool_call_title.__file__).resolve().parent
#: The owners, lowest layer first.
OWNERS = (
    "kiro_crew.tool_call_title_shell_parse",
    "kiro_crew.tool_call_title_shell_classify",
)
_THIS_FILE = Path(__file__).resolve()


def _owner_path(owner: str) -> Path:
    return SRC_DIR / f"{owner.rsplit('.', 1)[1]}.py"


def _defined(path: Path) -> set[str]:
    """Top-level names a module defines (def, class, assignment)."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return found


def _owner_reads(path: Path) -> set[str]:
    """Every name an owner's code loads: the names a patch would have to reach there."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _static_bindings(path: Path) -> dict[str, str]:
    """Name -> module, for every module-level ``from <module> import`` in *path*."""
    found: dict[str, str] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found[alias.asname or alias.name] = node.module
    return found


def test_the_owners_are_the_shell_siblings_in_layer_order() -> None:
    """Each owner imports only the owners listed before it, and none imports the facade."""
    on_disk = sorted(f"kiro_crew.{p.stem}" for p in SRC_DIR.glob("tool_call_title_*.py"))
    assert on_disk == sorted(OWNERS)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        later = sorted(m for m in imported if m in OWNERS and OWNERS.index(m) >= position)
        if FACADE in imported:
            later.append(FACADE)
        if later:
            late[owner] = later
    assert late == {}


def test_every_owner_defined_public_name_is_bound_on_the_facade_as_the_owner_object() -> None:
    """``from kiro_crew.tool_call_title import tokenize_shell`` keeps working, keeps
    type-checking (the facade imports the name itself) and is the owner's object."""
    bound = _static_bindings(Path(tool_call_title.__file__))
    missing: dict[str, list[str]] = {}
    for owner in OWNERS:
        module = importlib.import_module(owner)
        public = sorted(n for n in _defined(_owner_path(owner)) if not n.startswith("_"))
        assert public, f"{owner} defines no public name -- the reader went blind"
        absent = [n for n in public if bound.get(n) != owner]
        if absent:
            missing[owner] = absent
        for name in public:
            assert vars(tool_call_title)[name] is vars(module)[name], name
    assert missing == {}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


def _patched_through_facade(text: str) -> set[str]:
    return facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "tool_call_title" in text:
            found |= _patched_through_facade(text)
    return frozenset(found)


def test_no_test_patches_through_the_facade_a_name_an_owner_reads() -> None:
    """A re-export binds a second name: a patch of ``tool_call_title.<name>`` rebinds the
    facade's binding and never the owner's, so the owner's code would keep the original.
    Until the facade forwards writes, no test may rely on such a patch."""
    owner_reads = set().union(*(_owner_reads(_owner_path(owner)) for owner in OWNERS))
    assert sorted(_corpus_patched_names() & owner_reads) == []


def test_the_patch_scan_answers_both_ways() -> None:
    """Non-vacuity for the check above: the scan reads each patch form through the facade,
    and ignores a patch of another module."""
    planted = (
        "from unittest import mock\n"
        "from kiro_crew import tool_call_title as tct\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(tct, "tokenize_shell", None)\n'
        '    with mock.patch("kiro_crew.tool_call_title.split_segments"):\n'
        "        pass\n"
        '    monkeypatch.setattr("kiro_crew.other.split_segments", None)\n'
    )
    assert _patched_through_facade(planted) == {"tokenize_shell", "split_segments"}


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_tool_call_title.py" in walked
