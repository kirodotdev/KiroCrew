"""The shared LLM turn helpers' facade keeps the patch seams the one module had.

``kiro_crew.llm_helpers`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owners in ``kiro_crew.llm_helpers_parts``. Callers, tests and documents still reach every
name as ``kiro_crew.llm_helpers.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the
``llm_helpers_parts`` modules, loaded lowest layer first; the facade keeps no code of
its own; every name it serves from outside the family stays visible to type checkers;
the family logs under the facade's name; the facade still runs as ``python -m``; and
the ACP-layer names the owners read come from the agent SDK's turn vocabulary, which is
the same objects and which no other module imports.
"""

from __future__ import annotations

import ast
import functools
import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, llm_helpers
from kiro_crew.agent_sdk.drivers import acp_turn_vocab
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_llm_helpers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.llm_helpers"
PARTS = "kiro_crew.llm_helpers_parts"
PARTS_DIR = Path(llm_helpers.__file__).resolve().parent / "llm_helpers_parts"
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: helpers have: a deny check and the scan ceiling it reads, the session-switch lock
#: table the conftest resets for every test, the streaming entry point consumers stub, a
#: retry curve, a fallback budget, a config class, the SEL factory and a module the whole
#: family reads. The scan below must find all of them, so a scanner that stops matching
#: goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "sensitive_path_refusal",
        "_first_tool_input_denial",
        "_title_denial",
        "_MAX_SCANNABLE_TOOL_INPUT_CHARS",
        "_slot_switch_session_locks",
        "stream_and_collect",
        "run_bg_oneliner",
        "transient_retry_delay",
        "FALLBACK_CANDIDATE_ATTEMPTS",
        "KiroCrewConfig",
        "is_sensitive_bash_command",
        "_sel",
        "time",
    }
)

#: Where each name of the agent SDK's turn vocabulary is defined: the facade's owners
#: read these by value, so each must be that very object.
_VOCABULARY_HOMES = {
    "AcpError": "kiro_crew.acp.client",
    "AcpPromptBusy": "kiro_crew.acp.client",
    "advertised_model_ids": "kiro_crew.acp.client",
    "EVENT_STEER_CONSUMED": "kiro_crew.acp.types",
    "PROVIDER_LABEL_DEFAULT": "kiro_crew.acp.types",
    "TurnUsage": "kiro_crew.acp.types",
    "EVENT_COMPLETE": "kiro_crew.providers.base",
    "EVENT_PERMISSION_REQUEST": "kiro_crew.providers.base",
    "EVENT_TEXT_CHUNK": "kiro_crew.providers.base",
    "EVENT_TOOL_CALL": "kiro_crew.providers.base",
    "EVENT_TOOL_RESULT": "kiro_crew.providers.base",
    "LLMEvent": "kiro_crew.providers.base",
    "LLMProvider": "kiro_crew.providers.base",
    "resolve_billing_stats": "kiro_crew.providers.base",
}


def _index() -> _facade_mirror.MirrorIndex:
    return llm_helpers.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{PARTS}.{path.stem}": path
        for path in sorted(PARTS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


def _family() -> list[ModuleType]:
    return [llm_helpers, *(sys.modules[name] for name in _index().owners)]


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
        if "llm_helpers" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``llm_helpers.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(llm_helpers, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(llm_helpers, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(llm_helpers, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_the_parts_package_in_layer_order() -> None:
    """``_OWNERS`` lists every ``llm_helpers_parts`` module, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
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


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``llm_helpers.py`` was a plain module, so no reader fetched a name of it through
    ``importlib.import_module``: the facade forwards no legacy table and keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_facade_keeps_no_code_of_its_own() -> None:
    """New work goes to the owner of its responsibility: the facade defines no function
    or class, so nothing lands in it that the owners' map does not name."""
    tree = ast.parse(Path(llm_helpers.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    assert "stream_and_collect" in _index().exports, "the facade serves nothing"


def test_the_facade_imports_each_owner_module_by_name() -> None:
    """The private owner aliases exist so a type checker loads every owner before this
    facade (an import cycle reads owner names at import time); they name exactly the
    owners in ``_OWNERS``, no more and no fewer."""
    aliased = {
        value.__name__
        for name, value in vars(llm_helpers).items()
        if name.startswith("_")
        and isinstance(value, ModuleType)
        and value.__name__.startswith(f"{PARTS}.")
    }
    assert aliased == set(_index().owners)


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    tree = ast.parse(Path(llm_helpers.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the module bound these by import, so ``from kiro_crew.llm_helpers import
    fire_tool_hooks`` type-checked. Now an owner holds them, so the facade names each one
    from its home module under ``TYPE_CHECKING``, and exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert llm_helpers.logger.name == FACADE
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", llm_helpers.logger) is not llm_helpers.logger
    ]
    assert others == []


def test_the_facade_runs_as_a_module(tmp_path: Path) -> None:
    """``python -m kiro_crew.llm_helpers`` imported cleanly before the split; the mirror
    installs on ``__main__`` too, because every member binds one logger by name."""
    env = dict(os.environ)
    src = str(Path(llm_helpers.__file__).resolve().parents[1])
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


def test_the_turn_vocabulary_is_the_acp_layers_own_objects() -> None:
    """The owners read these names by value; each is the object its home module binds,
    so an ``except AcpError`` or an ``event.kind == EVENT_COMPLETE`` in an owner means
    what it meant when the module imported them from the ACP layer itself."""
    assert sorted(acp_turn_vocab.__all__) == sorted(_VOCABULARY_HOMES)
    wrong = [
        name
        for name, home in _VOCABULARY_HOMES.items()
        if getattr(acp_turn_vocab, name) is not getattr(importlib.import_module(home), name)
    ]
    assert wrong == []
    assert llm_helpers.AcpError is getattr(
        importlib.import_module("kiro_crew.acp.client"), "AcpError"
    )


def _imports(text: str, module: str, target: str, *, is_package: bool = False) -> bool:
    """Whether *text*, the source of module *module*, imports *target* outside ``if TYPE_CHECKING:``.

    Every spelling counts: ``from target import x``, ``from <package> import <leaf>``, ``import
    target`` (an alias or a submodule included), a relative import that resolves to it, and a
    literal ``importlib.import_module(target)`` or ``__import__(target)``, at any depth.
    """
    tree = ast.parse(text)
    typing_only = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }
    head, _, leaf = target.rpartition(".")
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                anchor = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            if base == target or (base == head and leaf in {a.name for a in node.names}):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name == target or a.name.startswith(f"{target}.") for a in node.names):
                return True
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == target
        ):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in {"import_module", "__import__"}:
                return True
    return False


def _runtime_importers_of(dotted: str) -> list[str]:
    """Modules under ``src/kiro_crew`` that import *dotted* outside ``if TYPE_CHECKING:``."""
    leaf = dotted.rpartition(".")[2]
    found = []
    for path, text, _tree in source_corpus.parsed_candidates(require_any=(leaf,)):
        rel = path.relative_to(source_corpus.src_root()).with_suffix("")
        is_package = rel.name == "__init__"
        module = ".".join(("kiro_crew", *rel.parts)).removesuffix(".__init__")
        if module != dotted and _imports(text, module, dotted, is_package=is_package):
            found.append(module)
    return sorted(found)


_SIBLING = "kiro_crew.agent_sdk.drivers.sibling"


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("from kiro_crew.agent_sdk.drivers.acp_turn_vocab import AcpError\n", True),
        ("from kiro_crew.agent_sdk.drivers import acp_turn_vocab\n", True),
        ("import kiro_crew.agent_sdk.drivers.acp_turn_vocab\n", True),
        ("import kiro_crew.agent_sdk.drivers.acp_turn_vocab as vocab\n", True),
        ("from .acp_turn_vocab import AcpError\n", True),
        ("from . import acp_turn_vocab\n", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.agent_sdk.drivers.acp_turn_vocab')\n",
            True,
        ),
        ("__import__('kiro_crew.agent_sdk.drivers.acp_turn_vocab')\n", True),
        ("def f():\n    from kiro_crew.agent_sdk.drivers.acp_turn_vocab import AcpError\n", True),
        (
            "if TYPE_CHECKING:\n    from kiro_crew.agent_sdk.drivers.acp_turn_vocab import LLMEvent\n",
            False,
        ),
        ("from kiro_crew.agent_sdk.drivers.acp_vocab import AcpError\n", False),
        ("from kiro_crew.agent_sdk.drivers import acp_vocab\n", False),
        ("from .acp_vocab import AcpError\n", False),
        ("# kiro_crew.agent_sdk.drivers.acp_turn_vocab\nNAME = 'acp_turn_vocab'\n", False),
    ],
)
def test_the_importer_scan_reads_every_spelling(text: str, flagged: bool) -> None:
    """The fence below is the only check on who imports the vocabulary, so its reader is
    held to every import spelling, both ways, on synthetic sources."""
    assert _imports(text, _SIBLING, acp_turn_vocab.__name__) is flagged


def test_only_the_owners_import_the_turn_vocabulary() -> None:
    """The vocabulary is the owners' one window onto the ACP layer, not a new way for
    the rest of the tree to reach it: outside ``if TYPE_CHECKING:``, only modules of
    ``kiro_crew.llm_helpers_parts`` import it."""
    importers = _runtime_importers_of(acp_turn_vocab.__name__)
    assert f"{PARTS}.turn_stream" in importers, "the importer scan went blind"
    assert [name for name in importers if not name.startswith(f"{PARTS}.")] == []


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_llm_helpers.py" in walked
