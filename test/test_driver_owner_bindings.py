"""``Driver`` keeps every member it had, and each is bound from the module that owns it.

The spine driver's publish and commit mechanics live in the ``spine/driver_*`` owner modules:
each such member's body is a module-level function there whose first parameter is ``self``,
and the class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The run's
state stays on the instance, so ``object.__new__(Driver)`` builds, instance overrides and
``Driver.<member>`` patches see the same object they always did. This file pins that contract:

* the frozen member inventory -- every member, its kind, its signature (the ``self``
  annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and an owner imports only owners below it;
* each owner's docstring names the instance fields its members write and read;
* a write through ``spine.driver`` reaches every owner that binds the name, for every name
  the test trees patch there (the facade's mirror, ``kiro_crew._facade_mirror``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew.apps.builtins.auto_improvement.spine import driver as facade
from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_driver_owner_bindings")

_PACKAGE = "kiro_crew.apps.builtins.auto_improvement.spine"

#: Member -> (kind, signature digest, owning module). Frozen at the split: the kind and the
#: signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, str, str]] = {
    "_PUSH_ATTEMPTS": ("data", "", ""),
    "__init__": ("function", "9edba68ad0b4", "driver"),
    "_apply_bug_winner": ("function", "29e12fb74d44", "driver"),
    "_apply_verdict": ("function", "10731d24da40", "driver"),
    "_build_test_pre_push_clean": ("function", "1933aa01e614", "driver_publish"),
    "_capture_profile": ("function", "7c9729d4d237", "driver"),
    "_commit_bug_winner_provisional": ("function", "b2da2093de2b", "driver_commit"),
    "_commit_winner_provisional": ("function", "b2da2093de2b", "driver_commit"),
    "_direct_push": ("function", "378402c78788", "driver_publish"),
    "_discard_staged": ("function", "21a176314740", "driver_commit"),
    "_fan_out_checked": ("function", "9adc4c9cf70c", "driver"),
    "_finalize_bug_winner_commit": ("function", "497b37788223", "driver_commit"),
    "_finalize_winner_commit": ("function", "f0e12fe129a1", "driver_commit"),
    "_head_is_the_committed_sha": ("function", "cde108f4876b", "driver_publish"),
    "_metric_blob": ("staticmethod", "028f84c1545c", "driver"),
    "_metric_direction": ("function", "bb418bb7ece8", "driver"),
    "_preflight_checked": ("function", "f4f1691e4bc6", "driver"),
    "_prepush_review_clean": ("function", "266d7cdd6dad", "driver_publish"),
    "_progress": ("function", "8bc2836d9525", "driver"),
    "_publishing_is_halted": ("function", "a6632a57e602", "driver_commit"),
    "_push_with_rebase": ("function", "ace6f4282dc2", "driver_publish"),
    "_quarantine_unrolled_clone": ("function", "82f6ed3e23e0", "driver_commit"),
    "_record": ("function", "8ea2c87d61c1", "driver"),
    "_redact_commit_message": ("staticmethod", "6decab3dc797", "driver_commit"),
    "_reset_provisional": ("function", "e452d80509de", "driver_commit"),
    "_resolve_measurement_boot": ("function", "f4ca892d9e9f", "driver"),
    "_restore_branch": ("function", "1931371d2561", "driver_publish"),
    "_retire_if_unsafe": ("function", "1476e3b4dff9", "driver"),
    "_reverify_head": ("function", "a6632a57e602", "driver_publish"),
    "_revision_scans_clean": ("function", "169094feab6f", "driver_publish"),
    "_stage_bug_winner": ("function", "b2da2093de2b", "driver_commit"),
    "_stage_winner": ("function", "b2da2093de2b", "driver_commit"),
    "_work_one_proposal": ("function", "b8de24397734", "driver"),
    "_work_one_proposal_checked": ("function", "6e752d3878ef", "driver"),
    "assert_push_disabled": ("function", "b60c5fea256e", "driver"),
    "head_sha": ("function", "bb418bb7ece8", "driver"),
    "preflight": ("function", "c47fb45f8d53", "driver"),
    "request_stop": ("function", "b60c5fea256e", "driver"),
    "run": ("function", "7deed9b2ae2a", "driver"),
    "run_cycle": ("function", "d5b49ba524b7", "driver"),
}

#: Class-level annotations the split adds: a field first set by a member an owner defines,
#: declared in the class so type checkers know it. An annotation creates no attribute.
_DECLARED = frozenset({"_pushed_object"})

#: The git helpers that moved from this module to ``driver_git``: the facade serves them.
_SERVED_GIT_HELPERS = frozenset(
    {
        "_ATTRIBUTION_FORMAT",
        "_GIT_SAFE_CONFIG",
        "_commit_attribution",
        "_commit_tree",
        "_committer_identity",
        "_expected_replay_tree",
        "_fetched_tip_oid",
        "_full_head_id",
    }
)


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in facade._OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(Driver)[name]
    return value.__func__ if isinstance(value, staticmethod) else value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(tree: ast.Module, owner: str = "") -> list[ast.FunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _digest, home) in _MEMBERS.items()
        if kind == "staticmethod" and home and f"{_PACKAGE}.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(Driver)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    annotations = set(Driver.__dict__.get("__annotations__", {}))
    assert sorted(present ^ set(_MEMBERS)) == []
    assert annotations == _DECLARED
    assert not _DECLARED & set(vars(Driver))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_signature_and_owner(name: str) -> None:
    kind, digest, owner = _MEMBERS[name]
    value = vars(Driver)[name]
    if kind == "data":
        assert not callable(value)
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert not inspect.iscoroutinefunction(fn)
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"{_PACKAGE}.{owner}"
    if owner != "driver":
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


def test_every_owner_is_listed_by_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _digest, owner in _MEMBERS.values() if owner not in ("", "driver")}
    listed = {name.rsplit(".", 1)[-1] for name in facade._OWNERS}
    assert homes <= listed
    assert listed == homes | {"driver_git"}


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_owner_defines_a_class(owner: str) -> None:
    """A class that moved would render its owner's dotted name in a traceback or a log line;
    every class the driver defines (``Driver``, ``BudgetCaps``, ``Stats``,
    ``PushEnabledError``) stays in the facade."""
    assert [n.name for n in _tree(sys.modules[owner]).body if isinstance(n, ast.ClassDef)] == []


def _class_only_constructs(fn: ast.FunctionDef) -> list[str]:
    """What breaks when a method body leaves its class: ``super()`` with no arguments, the
    ``__class__`` cell, and a name-mangled ``__attr`` (``__x`` without a trailing ``__``)."""
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        if isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        spelled = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(spelled, str) and spelled.startswith("__") and not spelled.endswith("__"):
            found.append(spelled)
    return found


def test_the_class_only_scan_can_fail() -> None:
    tree = ast.parse("def f(self):\n    super().g()\n    return __class__, self.__hidden\n")
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert sorted(_class_only_constructs(fn)) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    fns = _self_functions(_tree(sys.modules[owner]), owner)
    assert {fn.name: _class_only_constructs(fn) for fn in fns} == {fn.name: [] for fn in fns}


def test_every_static_member_is_scanned_as_bound() -> None:
    statics = {
        name: f"{_PACKAGE}.{home}"
        for name, (kind, _digest, home) in _MEMBERS.items()
        if kind == "staticmethod" and home != "driver"
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


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
        "if TYPE_CHECKING:\n    from .driver import Driver\n"
        "def f():\n    from .driver import x\n"
        f"import {_PACKAGE}.driver\n"
        f"from {_PACKAGE} import driver\n"
        "from . import driver_git as _g, ledger\n"
        f"from {_PACKAGE}.driver_publish import y\n"
    )
    assert _imported_spine_modules(tree, runtime_only=True) == {
        "driver": 5,
        "driver_git": 8,
        "ledger": 8,
        "driver_publish": 9,
    }
    assert _imported_spine_modules(tree, runtime_only=False)["driver"] == 3


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert "driver" not in _imported_spine_modules(_tree(sys.modules[owner]), runtime_only=True)


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners earlier
    in the tuple, so the order is the import order."""
    leaves = [name.rsplit(".", 1)[-1] for name in facade._OWNERS]
    later = {
        leaf: sorted(
            set(_imported_spine_modules(_tree(sys.modules[owner]), runtime_only=True))
            & (set(leaves) - set(leaves[:i]))
        )
        for i, (leaf, owner) in enumerate(zip(leaves, facade._OWNERS))
    }
    assert later == {leaf: [] for leaf in leaves}


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset({"add", "discard", "append", "pop", "update", "clear", "setdefault"})


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the attribute
    or of a subscript on it, a mutator call on it and ``setattr(self, "<f>", ...)``. Reads:
    every other load and ``getattr``/``hasattr(self, "<f>")``. Members are not fields."""
    written: set[str] = set()
    read: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                target = _self_field(node.value)
                if target is not None:
                    written.add(target)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                target = _self_field(func.value)
                if target is not None:
                    written.add(target)
            if (
                isinstance(func, ast.Name)
                and func.id in ("getattr", "hasattr", "setattr")
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                (written if func.id == "setattr" else read).add(node.args[1].value)
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    self.run()\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert _fields([fn]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", [o for o in facade._OWNERS if not o.endswith("driver_git")])
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert written | read, f"{owner} binds no member that touches the instance"
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


# --------------------------------------------------------------------------- #
# The mirror: a write through the facade reaches every owner that binds the name.
# --------------------------------------------------------------------------- #


def _index() -> Any:
    from kiro_crew import _facade_mirror

    return vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]


def test_the_mirror_serves_the_moved_git_helpers_and_excludes_nothing() -> None:
    index = _index()
    assert index.excluded == ()
    assert index.divergent() == ()
    assert set(facade._EXPORTS) == _SERVED_GIT_HELPERS
    assert set(facade._EXPORTS.values()) == {f"{_PACKAGE}.driver_git"}
    git_owner = sys.modules[f"{_PACKAGE}.driver_git"]
    for name in sorted(_SERVED_GIT_HELPERS):
        assert getattr(facade, name) is vars(git_owner)[name], name
        assert name not in vars(facade), f"{name} is bound on the facade and would shadow"


def test_no_owner_member_is_served_through_the_facade() -> None:
    """The facade's surface is what it bound before the split, so an owner's bound member is
    reached as ``Driver.<member>``, never as a new ``driver.<member>``."""
    for name, (_kind, _digest, owner) in _MEMBERS.items():
        if owner not in ("", "driver"):
            assert not hasattr(facade, name), name


def _test_tree_texts() -> list[tuple[str, str]]:
    """(path, text) of every test-tree file that names the driver module: the root conftest,
    ``test/``, and the builtin apps' test packages."""
    root = source_corpus.repo_root()
    test_root = root / "test"
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        in_tree = rel == "conftest.py" or path.is_relative_to(test_root)
        in_tree = in_tree or (rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel)
        if not in_tree:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if f"{_PACKAGE}.driver" in text or f"{_PACKAGE} import driver" in text:
            found.append((rel, text))
    return found


def _patched_through_the_facade() -> set[str]:
    spellings = {facade.__name__: facade.__name__}
    names: set[str] = set()
    for _rel, text in _test_tree_texts():
        try:
            names |= facade_mirror_scan.patched_names(text, spellings).get(facade.__name__, set())
        except SyntaxError:
            continue
    return names


def _reads(module: ModuleType) -> set[str]:
    tree = _tree(module)
    imports = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.lineno not in imports
    }


def _reach_gaps(patched: set[str], index: Any) -> list[str]:
    gaps = []
    for module in _owners():
        for name in sorted(patched & _reads(module)):
            if name in vars(module) and module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan sees the patches the moved code depends on: the git chokepoint the push
    # retries fake, and the coverage fixture's subprocess and pinning doubles.
    assert {"_git", "subprocess", "require_pinned", "time", "_run_dry"} <= patched
    assert _reach_gaps(patched, _index()) == []


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the test trees as data, so a test-only diff must still run it."""
    import importlib

    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_the_reach_scan_can_fail() -> None:
    index = _index()
    owner = f"{_PACKAGE}.driver_publish"
    holders = dict(index.holders)
    holders["_git"] = tuple(h for h in holders["_git"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"_git"}, index) == []
    assert _reach_gaps({"_git"}, blind) == [f"{owner}._git"]


@pytest.mark.parametrize("name", ["_git", "subprocess", "require_pinned", "_fetched_tip_oid"])
def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    holders = [m for m in _owners() if name in vars(m)]
    assert holders, f"no owner binds {name}"
    before = {m.__name__: vars(m)[name] for m in holders}
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(facade, name, sentinel)
        assert {m.__name__: vars(m)[name] for m in holders} == {
            m.__name__: sentinel for m in holders
        }
        assert getattr(facade, name) is sentinel
    assert {m.__name__: vars(m)[name] for m in holders} == before
    assert getattr(facade, name) is before[holders[0].__name__]


#: Loads the driver the way the gateway loads an app hook, under the app loader's own module
#: name, and reports what the facade's mirror indexed.
_APP_LOADER_PROBE = """
import json, sys
from pathlib import Path
import kiro_crew.apps.builtins
from kiro_crew.apps.module_loader import load_app_module
root = Path(kiro_crew.apps.builtins.__file__).resolve().parent / "auto_improvement"
main = load_app_module("auto-improvement", root, "spine.driver:main")
facade = sys.modules[main.__module__]
print(json.dumps({"module": main.__module__, "owners": list(facade._OWNERS),
                  "served": sorted(facade._EXPORTS)}))
"""


def test_the_driver_loads_under_the_app_loader_name(tmp_path: Path) -> None:
    """``load_app_module`` registers the app's files under its own package name, so the
    facade's owners are named from ``__package__``, not from the canonical dotted path.
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
    assert result["served"] == sorted(_SERVED_GIT_HELPERS)
