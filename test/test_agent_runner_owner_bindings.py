"""``spine.agent_runner`` keeps every name it bound, and a patch through it reaches the owners.

The agent runner's tool-request gate, activity-feed shaping and fix authoring live in the
``spine/agent_runner_*`` owner modules beside it. ``AgentRunner``, ``SessionAgentRunner``
and the helpers only they call stay in ``agent_runner.py``, which is the import path
callers use and serves a ``kiro_crew._facade_mirror`` mirror over the owners. This file
pins that contract:

* the owner set is exactly the ``agent_runner_*`` modules beside the facade, and each owner
  defines the names frozen here;
* every moved name still resolves through the facade, to the object its owner holds; the
  facade serves exactly the moved names it does not import;
* no owner imports the facade at run time or another owner, and no production module
  outside the family imports an owner;
* the family logs through one logger named for the facade;
* a write through ``spine.agent_runner`` reaches every owner that binds the name, for every
  name the test trees patch there;
* the family loads under the app loader's module name too.

Each structural scan has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.auto_improvement.spine import agent_runner as facade
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_agent_runner_owner_bindings")

_PACKAGE = "kiro_crew.apps.builtins.auto_improvement.spine"
_FACADE = f"{_PACKAGE}.agent_runner"
_THIS_FILE = Path(__file__).resolve()

#: Owner leaf -> the top-level names it defines, its logger aside. Frozen at the split, in
#: the facade's ``_OWNERS`` order.
_DEFINED: dict[str, frozenset[str]] = {
    "agent_runner_gate": frozenset(
        {
            "_COMMAND_WRAPPERS",
            "_FORBIDDEN_BINARIES",
            "_FORBIDDEN_SUBCOMMANDS",
            "_GovernanceDeny",
            "_MAX_UNWRAP_DEPTH",
            "_SHELL_BINARIES",
            "_VALUE_TAKING_OPTIONS",
            "_WRAPPER_VALUE_TAKING_LONG_OPTIONS",
            "_allowlist_deny_reason",
            "_bare_event",
            "_governance_denial",
            "_refusal",
            "_requested_command",
            "_shell_words",
            "_tool_permitted",
            "shell_command_refusal",
        }
    ),
    "agent_runner_feed": frozenset(
        {
            "_LINE_FLUSH_LEN",
            "_LineBuffer",
            "_TOOL_TARGET_KEYS",
            "_detail_is_richer",
            "_summarize_stream_event",
            "_tool_detail",
        }
    ),
    "agent_runner_authoring": frozenset(
        {
            "_GIT_SAFE_CONFIG",
            "_UNFINISHED_TURN_ERROR",
            "_adopt_authored_test",
            "_repro_test_dir",
            "author_bug_fix",
            "author_perf_fix",
        }
    ),
}

#: The moved names the facade's own code reads, so it imports them from their owner.
_IMPORTED_BY_THE_FACADE = frozenset(
    {
        "_GovernanceDeny",
        "_LineBuffer",
        "_UNFINISHED_TURN_ERROR",
        "_allowlist_deny_reason",
        "_bare_event",
        "_detail_is_richer",
        "_governance_denial",
        "_requested_command",
        "_summarize_stream_event",
        "_tool_detail",
        "_tool_permitted",
        "shell_command_refusal",
    }
)

#: Names the test trees patch through the facade that an OWNER reads as a global: the hook
#: layer ``_governance_denial`` builds, and the pin the fix-authoring presence check takes.
#: They are why the facade mirrors; the scan must find them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_OWNER_READ_SEAMS = frozenset(
    {"HookManager", "KiroCrewConfig", "hooks_config_from_config_dict", "require_pinned"}
)


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in facade._OWNERS]


def _index() -> Any:
    return vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]


def _source(module: ModuleType) -> str:
    return Path(module.__file__ or "").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The owner set and what each owner defines
# --------------------------------------------------------------------------- #


def test_the_facade_mirrors_exactly_the_sibling_owners() -> None:
    assert type(facade) is _facade_mirror.MirroredModule
    on_disk = sorted(
        f"{_PACKAGE}.{path.stem}"
        for path in Path(facade.__file__ or "").resolve().parent.glob("agent_runner_*.py")
    )
    assert sorted(facade._OWNERS) == on_disk
    assert [name.rpartition(".")[2] for name in facade._OWNERS] == list(_DEFINED)
    index = _index()
    assert index.excluded == ()
    assert index.divergent() == ()


@pytest.mark.parametrize("leaf", list(_DEFINED))
def test_each_owner_defines_its_frozen_names(leaf: str) -> None:
    module = sys.modules[f"{_PACKAGE}.{leaf}"]
    defined = _facade_mirror._defined_names(ast.parse(_source(module))) - {"logger"}
    assert sorted(defined) == sorted(_DEFINED[leaf])


def test_every_moved_name_resolves_through_the_facade_to_its_owner() -> None:
    for leaf, names in _DEFINED.items():
        owner = vars(sys.modules[f"{_PACKAGE}.{leaf}"])
        assert sorted(n for n in names if getattr(facade, n) is not owner[n]) == [], leaf


def test_the_facade_serves_exactly_the_moved_names_it_does_not_import() -> None:
    moved = frozenset().union(*_DEFINED.values())
    served = moved - _IMPORTED_BY_THE_FACADE
    assert sorted(facade._EXPORTS) == sorted(served)
    assert sorted(name for name in served if name in vars(facade)) == [], "would shadow"
    for name in sorted(_IMPORTED_BY_THE_FACADE):
        holders = _index().holders[name]
        assert vars(facade)[name] is vars(sys.modules[holders[0]])[name], name


# --------------------------------------------------------------------------- #
# Import direction
# --------------------------------------------------------------------------- #


def _imported_spine_modules(tree: ast.Module, runtime_only: bool) -> dict[str, int]:
    """Spine module leaf -> the first line importing it, absolute or relative. With
    *runtime_only*, imports inside an ``if TYPE_CHECKING:`` block do not count."""
    guarded: set[int] = set()
    if runtime_only:
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
                guarded.update(id(inner) for inner in ast.walk(node))
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        leaves: list[str] = []
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level == 1 and base:
                leaves.append(base.split(".")[0])
            elif node.level == 1:
                leaves.extend(alias.name for alias in node.names)
            elif base.startswith(f"{_PACKAGE}."):
                leaves.append(base[len(_PACKAGE) + 1 :].split(".")[0])
            elif base == _PACKAGE:
                leaves.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            leaves.extend(
                alias.name[len(_PACKAGE) + 1 :].split(".")[0]
                for alias in node.names
                if alias.name.startswith(f"{_PACKAGE}.")
            )
        for leaf in leaves:
            found[leaf] = min(found.get(leaf, node.lineno), node.lineno)
    return found


def test_the_import_scan_reads_every_spelling_and_skips_type_checking() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from .agent_runner import AgentRunner\n"
        "def f():\n    from .agent_runner import x\n"
        f"import {_PACKAGE}.agent_runner\n"
        f"from {_PACKAGE} import agent_runner\n"
        "from . import agent_runner_feed as _f, git_safety\n"
        f"from {_PACKAGE}.agent_runner_gate import y\n"
    )
    assert _imported_spine_modules(tree, runtime_only=True) == {
        "agent_runner": 5,
        "agent_runner_feed": 8,
        "git_safety": 8,
        "agent_runner_gate": 9,
    }
    assert _imported_spine_modules(tree, runtime_only=False)["agent_runner"] == 3


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_owner_imports_the_facade_or_another_owner_at_run_time(owner: str) -> None:
    imported = _imported_spine_modules(ast.parse(_source(sys.modules[owner])), True)
    family = {"agent_runner", *_DEFINED}
    assert sorted(set(imported) & family) == []


def _owner_importers(sources: dict[str, str]) -> dict[str, list[str]]:
    """Module -> the owner leaves it imports, for every module outside the family; relative
    spellings resolved against the module's own dotted name."""
    family = {_FACADE, *(f"{_PACKAGE}.{leaf}" for leaf in _DEFINED)}
    found: dict[str, list[str]] = {}
    for module, source in sources.items():
        if module in family:
            continue
        reached: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    head = module.split(".")[: -node.level]
                    base = ".".join([*head, *([node.module] if node.module else [])])
                reached |= {base, *(f"{base}.{alias.name}" for alias in node.names)}
            elif isinstance(node, ast.Import):
                reached |= {alias.name for alias in node.names}
        owners = sorted(
            name.rpartition(".")[2]
            for name in reached
            if name.rpartition(".")[0] == _PACKAGE and name.rpartition(".")[2] in _DEFINED
        )
        if owners:
            found[module] = owners
    return found


def test_only_the_family_imports_an_owner_in_production_code() -> None:
    """Callers import ``spine.agent_runner``; an owner is the family's own business."""
    root = source_corpus.src_root()
    sources = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "agent_runner_" in text or path.stem.startswith("agent_runner_"):
            parts = path.relative_to(root).with_suffix("").parts
            sources[".".join((root.name, *parts))] = text
    assert set(facade._OWNERS) <= set(sources), "the scan no longer sees the owners"
    assert _owner_importers(sources) == {}


def test_the_owner_import_scan_finds_an_outside_importer() -> None:
    planted = {
        "kiro_crew.planted_a": f"from {_PACKAGE}.agent_runner_gate import _refusal\n",
        "kiro_crew.planted_b": f"from {_PACKAGE} import agent_runner_feed\n",
        "kiro_crew.planted_c": f"import {_PACKAGE}.agent_runner_authoring\n",
        f"{_PACKAGE.rpartition('.')[0]}.backend.planted": "from ..spine import agent_runner_gate\n",
        f"{_PACKAGE}.planted_d": "from .agent_runner_feed import _LineBuffer\n",
        f"{_PACKAGE}.agent_runner_authoring": "from .agent_runner_gate import _refusal\n",
    }
    assert _owner_importers(planted) == {
        "kiro_crew.planted_a": ["agent_runner_gate"],
        "kiro_crew.planted_b": ["agent_runner_feed"],
        "kiro_crew.planted_c": ["agent_runner_authoring"],
        f"{_PACKAGE.rpartition('.')[0]}.backend.planted": ["agent_runner_gate"],
        f"{_PACKAGE}.planted_d": ["agent_runner_feed"],
    }


# --------------------------------------------------------------------------- #
# One logger, and the classes that stay
# --------------------------------------------------------------------------- #


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """A record the moved code logs carries the facade's logger name, as it did, and the
    mirror holds one object for ``logger``."""
    assert facade.logger is logging.getLogger(facade.__name__)
    loggers = [m.__name__ for m in _owners() if "logger" in vars(m)]
    assert loggers == [f"{_PACKAGE}.agent_runner_gate", f"{_PACKAGE}.agent_runner_authoring"]
    stray = [name for name in loggers if vars(sys.modules[name])["logger"] is not facade.logger]
    assert stray == []


def test_the_runners_keep_the_facade_module() -> None:
    """What a traceback, a log line or a repr names: the runners never left the facade."""
    for cls in (facade.AgentResult, facade.AgentRunner, facade.SessionAgentRunner):
        assert cls.__module__ == facade.__name__, cls


# --------------------------------------------------------------------------- #
# The mirror: a write through the facade reaches every owner that binds the name.
# --------------------------------------------------------------------------- #


def _test_tree_texts() -> list[tuple[str, str]]:
    """(path, text) of every test-tree file that names the facade: the root conftest,
    ``test/`` and the builtin apps' test packages, less this file."""
    root = source_corpus.repo_root()
    test_root = root / "test"
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        in_tree = rel == "conftest.py" or path.is_relative_to(test_root)
        in_tree = in_tree or (rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel)
        if not in_tree or path.resolve() == _THIS_FILE:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _FACADE in text or f"{_PACKAGE} import agent_runner" in text:
            found.append((rel, text))
    return found


def _patched_through_the_facade() -> set[str]:
    spellings = {_FACADE: _FACADE}
    names: set[str] = set()
    for _rel, text in _test_tree_texts():
        try:
            names |= facade_mirror_scan.patched_names(text, spellings).get(_FACADE, set())
        except SyntaxError:
            continue
    return names


def _reach_gaps(patched: set[str], index: Any) -> list[str]:
    """``owner.name`` for each patched name an owner reads and binds but the index does not
    route to it, so a patch through the facade would miss that owner."""
    gaps = []
    for module in _owners():
        reads = facade_mirror_scan.global_reads(_source(module), module.__name__)
        for name in sorted(patched & reads):
            if name in vars(module) and module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    assert sorted(_OWNER_READ_SEAMS - patched) == []
    assert _reach_gaps(patched, _index()) == []


def test_the_reach_scan_can_fail() -> None:
    index = _index()
    owner = f"{_PACKAGE}.agent_runner_gate"
    holders = dict(index.holders)
    holders["HookManager"] = tuple(h for h in holders["HookManager"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"HookManager"}, index) == []
    assert _reach_gaps({"HookManager"}, blind) == [f"{owner}.HookManager"]


@pytest.mark.parametrize(
    "name",
    ["HookManager", "KiroCrewConfig", "require_pinned", "logger", "_governance_denial"]
    + ["author_bug_fix"],
)
def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    holders = [m for m in _owners() if name in vars(m)]
    assert holders, f"no owner binds {name}"
    bound_here = name in vars(facade)
    before = {m.__name__: vars(m)[name] for m in holders}
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(facade, name, sentinel)
        assert {m.__name__: vars(m)[name] for m in holders} == {
            m.__name__: sentinel for m in holders
        }
        assert getattr(facade, name) is sentinel
    assert {m.__name__: vars(m)[name] for m in holders} == before
    assert (name in vars(facade)) is bound_here
    assert getattr(facade, name) is before[holders[0].__name__]


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the test trees as data, so a test-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)


#: Loads the runner the way the gateway's app backend reaches it, under the app loader's own
#: module name, and reports what the facade's mirror indexed and how the family logs.
_APP_LOADER_PROBE = """
import json, sys
from pathlib import Path
import kiro_crew.apps.builtins
from kiro_crew.apps.module_loader import load_app_module
root = Path(kiro_crew.apps.builtins.__file__).resolve().parent / "auto_improvement"
runner = load_app_module("auto-improvement", root, "spine.agent_runner:SessionAgentRunner")
facade = sys.modules[runner.__module__]
owners = [sys.modules[name] for name in facade._OWNERS]
print(json.dumps({"module": runner.__module__, "owners": list(facade._OWNERS),
                  "served": sorted(facade._EXPORTS), "logger": facade.logger.name,
                  "owner_loggers": sorted({vars(m)["logger"].name for m in owners
                                           if "logger" in vars(m)})}))
"""


def test_the_runner_loads_under_the_app_loader_name(tmp_path: Path) -> None:
    """``load_app_module`` registers the app's files under its own package name, so the
    owners and the logger are named from ``__package__``, not from the canonical dotted path.
    Checked in a fresh interpreter, so this worker's ``sys.modules`` gains no app copy."""
    env = dict(os.environ)
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", _APP_LOADER_PROBE],
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        **UTF8_TEXT,
    )
    result = json.loads(out.stdout.splitlines()[-1])
    package = result["module"].rpartition(".")[0]
    assert package != facade.__package__
    assert result["owners"] == [f"{package}.{name.rpartition('.')[2]}" for name in facade._OWNERS]
    assert result["served"] == sorted(facade._EXPORTS)
    assert result["owner_loggers"] == [result["logger"]] == [result["module"]]
