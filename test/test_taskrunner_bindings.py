"""``TaskRunner`` keeps every member it had, and each is bound from the module that owns it.

The runner's behaviour lives in the ``kiro_crew.taskrunner_parts`` owner modules: each member's
body is a module-level function there whose first parameter is ``self``, and the class binds it
by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The instance state stays on the
instance, so tests, ``TaskRunner.__new__`` builds and the unbound calls with a stand-in ``self``
see the same object they always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and each imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* owners log under the facade's logger, and a class moved to an owner keeps the facade as its
  ``__module__``;
* a write through ``kiro_crew.taskrunner`` reaches every owner that reads the name, for every
  name the test trees patch there (the facade's mirror, ``_facade_mirror``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import taskrunner
from kiro_crew.taskrunner import TaskRunner

pytestmark = pytest.mark.xdist_group(name="tree_scan_taskrunner_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now
#: (``taskrunner`` for the class body itself).
_MEMBERS: dict[str, tuple[str, bool, str, str]] = {
    "_RUNS_FILE": ("data", False, "", "taskrunner"),
    "__init__": ("function", False, "193abdb915b4", "taskrunner"),
    "_admission_closed": ("function", False, "a6632a57e602", "run_lifecycle"),
    "_apersist_runs": ("function", True, "b60c5fea256e", "run_registry"),
    "_auto_name": ("staticmethod", False, "e54a7ccb36d7", "plan_mode"),
    "_bind_run_execution": ("function", True, "61edd429c71b", "run_scope"),
    "_bound_history_key": ("function", True, "ec09f2c0755e", "run_scope"),
    "_build_resume_context": ("function", False, "41bb689ce7c8", "run_registry"),
    "_build_task_prompt": ("function", True, "4c406dd14756", "run_steps"),
    "_call_llm_for_lesson": ("function", True, "1a3c02a39576", "run_reporting"),
    "_capture_execution": ("function", False, "d1b2657e6a4e", "run_scope"),
    "_clamp_parallel_steps": ("staticmethod", False, "2edea9f8215c", "run_inputs"),
    "_cleanup_run_sessions": ("function", True, "859f30be3e40", "run_scope"),
    "_commit_snapshot": ("function", False, "04bd86086d4a", "run_registry"),
    "_decompose": ("function", True, "4b43246cbda4", "plan_mode"),
    "_ensure_resumable_workspace": ("function", True, "c658adc36b67", "run_lifecycle"),
    "_execute_single_task": ("function", True, "b60a0d3a7a41", "run_steps"),
    "_execute_tasks": ("function", True, "2bd591d8085c", "run_steps"),
    "_extract_lesson": ("function", True, "24d670a02d2c", "run_reporting"),
    "_grant_run_trust": ("function", True, "3c343146e57d", "run_scope"),
    "_group_parallel_tasks": ("staticmethod", False, "2c337d19d4fd", "plan_mode"),
    "_load_checkpoint": ("function", False, "92d4fec79650", "run_registry"),
    "_load_runs": ("function", False, "b60c5fea256e", "run_registry"),
    "_log_task": ("function", False, "8eb91f39d77e", "run_reporting"),
    "_mark_workflow_adapted": ("staticmethod", False, "970a3b72e24a", "workflow_port"),
    "_next_persist_seq": ("function", False, "fa6cfecd5574", "run_registry"),
    "_normalize_cross_group_deps": ("staticmethod", False, "6480f258070a", "plan_mode"),
    "_notify": ("function", True, "f44be43c94ec", "run_reporting"),
    "_parse_tasks": ("function", False, "2194d0d5a170", "plan_mode"),
    "_persist_runs": ("function", False, "b60c5fea256e", "run_registry"),
    "_refresh_from_config": ("function", False, "b60c5fea256e", "run_inputs"),
    "_release_run_runtime": ("function", True, "859f30be3e40", "run_scope"),
    "_release_start": ("function", False, "2b842e351101", "run_lifecycle"),
    "_require_workflow_ready": ("function", False, "b60c5fea256e", "workflow_port"),
    "_reserve_start": ("function", True, "2b842e351101", "run_lifecycle"),
    "_reset_incomplete_tasks": ("staticmethod", False, "970a3b72e24a", "run_scope"),
    "_resolve_task": ("function", False, "b608d38595e3", "plan_mode"),
    "_runs_path": ("function", False, "b08d8e6a45bd", "run_registry"),
    "_save_progress": ("function", False, "859f30be3e40", "run_registry"),
    "_serialize_runs": ("function", False, "bb418bb7ece8", "run_registry"),
    "_taskq_admit_step": ("function", True, "26f0eeb8d4b4", "taskq_bridge"),
    "_taskq_begin_run": ("function", True, "859f30be3e40", "taskq_bridge"),
    "_taskq_end_run": ("function", True, "859f30be3e40", "taskq_bridge"),
    "_taskq_lane_inputs": ("function", False, "22249aa59807", "taskq_bridge"),
    "_taskq_row_state": ("function", True, "adb963a7a81b", "taskq_bridge"),
    "_try_replan": ("function", True, "26c212c5b9bf", "run_steps"),
    "_watchdog_loop": ("function", True, "859f30be3e40", "run_steps"),
    "_workflow_begin": ("function", True, "56bc457efaba", "workflow_port"),
    "_workflow_delete_link": ("function", True, "859f30be3e40", "workflow_port"),
    "_workflow_finalize": ("function", True, "859f30be3e40", "workflow_port"),
    "_workflow_persist_replacement": ("function", True, "3ff49dc4901e", "workflow_port"),
    "_workflow_rebind": ("function", True, "859f30be3e40", "workflow_port"),
    "_workflow_set_plan": ("function", True, "c3356f47533a", "workflow_port"),
    "adopt_task_rows": ("function", True, "f540ddf6da0a", "taskq_bridge"),
    "attach_task_admission": ("function", False, "a1392cfcab90", "taskq_bridge"),
    "attach_workflow_service": ("function", False, "91113ebd1b19", "workflow_port"),
    "cancel": ("function", False, "241c3dd35dec", "run_lifecycle"),
    "cancel_plan": ("function", False, "b60c5fea256e", "plan_mode"),
    "current_run": ("property", False, "d9897e592c2e", "taskrunner"),
    "defer_workflow_attachment": ("function", False, "f14c5ff17c14", "workflow_port"),
    "delete_run": ("function", True, "ae22938dd735", "run_lifecycle"),
    "execute_plan": ("function", True, "c7716637826e", "taskrunner"),
    "pause": ("function", False, "2b842e351101", "run_lifecycle"),
    "plan": ("function", True, "75ded80d96af", "plan_mode"),
    "plan_to_chat_context": ("function", False, "5e6fa4118b91", "plan_mode"),
    "retry_from_task": ("function", True, "4b92d34b83ba", "taskrunner"),
    "run": ("function", True, "009afad1e01c", "run_lifecycle"),
    "running": ("property", False, "a6632a57e602", "taskrunner"),
    "self_review": ("function", True, "bcadeb840115", "run_steps"),
    "start_background": ("function", True, "46d9c2194fcb", "run_lifecycle"),
    "start_workflow_definition": ("function", True, "aee20a6f3206", "plan_mode"),
    "status": ("function", False, "44dc9acb7120", "run_registry"),
    "task_admission": ("property", False, "48800026c1b4", "taskrunner"),
    "update_plan": ("function", True, "c0b6a90d1a7d", "plan_mode"),
    "update_task": ("function", True, "326f8db3d128", "plan_mode"),
}

#: Class-level annotations the split adds: fields first set by a member an owner defines,
#: declared in the class so type checkers know them. An annotation creates no attribute.
_DECLARED = frozenset({"_workflow_initialization_error"})

_PACKAGE = "kiro_crew.taskrunner_parts"


def _module_of(owner: str) -> str:
    return taskrunner.__name__ if owner == "taskrunner" else f"{_PACKAGE}.{owner}"


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in taskrunner._OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(TaskRunner)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(
    tree: ast.Module, owner: str = ""
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The module-level functions of *tree* that a class binds: a first parameter ``self``, or
    one the inventory records as a static method of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and _module_of(home) == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(TaskRunner)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    present |= set(TaskRunner.__dict__.get("__annotations__", {}))
    assert sorted(present ^ (set(_MEMBERS) | _DECLARED)) == []
    assert not _DECLARED & set(vars(TaskRunner))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind == "data":
        assert name in vars(TaskRunner)
        return
    value = vars(TaskRunner)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == _module_of(owner)
    if owner != "taskrunner":
        assert vars(sys.modules[_module_of(owner)])[name] is fn


def test_every_owner_is_listed_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner != "taskrunner"}
    listed = {name.rsplit(".", 1)[-1] for name in taskrunner._OWNERS}
    assert homes == listed


@pytest.mark.parametrize("owner", taskrunner._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What only works inside a class body: zero-argument ``super()``, the implicit
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
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", taskrunner._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def test_the_bound_function_scans_include_every_static_method() -> None:
    statics = {
        name: _module_of(home)
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod"
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == "kiro_crew.taskrunner" or (
                node.module == "kiro_crew" and any(a.name == "taskrunner" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == "kiro_crew.taskrunner" for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.taskrunner import TaskRunner\n"
        "def f():\n    from kiro_crew.taskrunner import x\n"
        "import kiro_crew.taskrunner\n"
        "from kiro_crew import taskrunner\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", taskrunner._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{_PACKAGE}."):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(f"{_PACKAGE}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(f"{_PACKAGE}."))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.taskrunner_parts.run_scope import X\n"
        "from kiro_crew.taskrunner_parts import run_inputs as _ri\n"
        "import kiro_crew.taskrunner_parts.run_steps\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.taskrunner_parts.run_scope",
        "kiro_crew.taskrunner_parts.run_inputs",
        "kiro_crew.taskrunner_parts.run_steps",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """Owners form one layering: an owner reads names only from owners earlier in the
    tuple, so the order is the import order."""
    owners = taskrunner._OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset({"add", "discard", "append", "pop", "update", "clear", "setdefault"})


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """``(written, read)`` instance fields of *fns*. Writes: a store or delete of the
    attribute or of a subscript on it, a mutator call on it and ``setattr(self, "<f>",
    ...)``. Reads: every other load and ``getattr``/``hasattr(self, "<f>")``."""
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
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    assert _fields([tree.body[0]]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", taskrunner._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", taskrunner._OWNERS)
def test_owners_log_under_the_taskrunner_logger(owner: str) -> None:
    module = sys.modules[owner]
    assert getattr(module, "logger", taskrunner.logger) is taskrunner.logger


@pytest.mark.parametrize("owner", taskrunner._OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """A class moved to an owner keeps naming the path callers import it from."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: taskrunner.__name__ for name in defined
    }


def test_the_initializing_refusal_names_the_facade() -> None:
    error = taskrunner.WorkflowInitializing("retry shortly", code="workflow_initializing")
    assert f"{type(error).__module__}.{type(error).__qualname__}" == (
        "kiro_crew.taskrunner.WorkflowInitializing"
    )
    assert isinstance(error, RuntimeError) and error.code == "workflow_initializing"


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #


def _test_tree_texts() -> list[tuple[str, str]]:
    root = source_corpus.repo_root()
    paths = [*(root / "test").rglob("*.py"), *(root / "src" / "kiro_crew" / "apps").rglob("*.py")]
    out = []
    for path in sorted(paths):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("src/") and "/tests/" not in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "taskrunner" in text:
            out.append((rel, text))
    return out


def _patched_through_the_facade() -> set[str]:
    spellings = {taskrunner.__name__: taskrunner.__name__}
    names: set[str] = set()
    for _rel, text in _test_tree_texts():
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(
            taskrunner.__name__, set()
        )
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


def _index() -> Any:
    from kiro_crew import _facade_mirror

    return vars(taskrunner)[_facade_mirror.INDEX_ATTRIBUTE]


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan saw the facade's patch corpus: these are patched through it at the split.
    assert {"git_coord", "compute_memory_sized_parallel_cap", "time", "sel"} <= patched
    assert _reach_gaps(patched, _index()) == []


def test_the_reach_scan_can_fail() -> None:
    index = _index()
    owner = "kiro_crew.taskrunner_parts.run_steps"
    holders = dict(index.holders)
    holders["git_coord"] = tuple(h for h in holders["git_coord"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"git_coord"}, index) == []
    assert _reach_gaps({"git_coord"}, blind) == [f"{owner}.git_coord"]


def test_the_mirror_indexes_one_object_per_name_and_serves_the_moved_names() -> None:
    index = _index()
    assert index.excluded == ()
    assert index.divergent() == ()
    assert set(taskrunner._EXPORTS) == set(taskrunner._MOVED_SURFACE)
    for name, owner in taskrunner._EXPORTS.items():
        assert getattr(taskrunner, name) is vars(sys.modules[owner])[name]


def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readers = [m for m in _owners() if "git_coord" in vars(m)]
    assert len(readers) >= 2
    before = taskrunner.git_coord
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(taskrunner, "git_coord", sentinel)
        assert [vars(m)["git_coord"] for m in readers] == [sentinel] * len(readers)
    assert [vars(m)["git_coord"] for m in readers] == [before] * len(readers)
    assert taskrunner.git_coord is before
