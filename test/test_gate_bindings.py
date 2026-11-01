"""``_GateMixin`` keeps every member it had, each bound from the ``gate_*`` owner that holds it.

The spawn gate's members other than ``spawn_impl`` live in three sibling owners of
``kiro_crew.subagent_manager.admission``: ``gate_execution``, ``gate_pressure`` and
``gate_reporting``. Each body is a module-level function whose first parameter is
``self``, and the class in ``admission/gate.py`` binds it by name, so ``_hoist_impls``,
``bind_component_globals`` and every test that reaches a member through the class see
the object they always did. This file pins that contract:

* the frozen member inventory: every member, its kind, its signature (the ``self``
  annotation aside) and the module that holds it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self``
  function;
* every ``*_impl`` member reaches the coordinator rebound onto ``kiro_crew.subagent``'s
  globals and every global it reads resolves there, so a patch of a ``kiro_crew.subagent``
  name reaches it; every other member runs on its owner's globals and every global it
  reads resolves there;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports ``admission/gate.py`` at run time, and nothing else in ``src`` imports
  an owner;
* each owner's docstring names the manager fields its members write and read;
* no name the test trees patch through ``kiro_crew.subagent_manager.admission.gate`` is
  read as a global by an owner.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import builtins
import dis
import functools
import importlib
import inspect
import re
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import Any

import pytest
from facade_mirror_scan import patched_names

import kiro_crew.subagent as subagent_module
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.admission import gate as gate_module
from kiro_crew.subagent_manager.admission.gate import _GateMixin

pytestmark = pytest.mark.xdist_group(name="tree_scan_gate_bindings")

_PACKAGE = "kiro_crew.subagent_manager.admission"
_GATE = f"{_PACKAGE}.gate"
_OWNERS = ("gate_execution", "gate_pressure", "gate_reporting")
_TEST_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_ROOT.parent

#: Member -> (kind, signature with the ``self`` annotation removed, owning module). The
#: kind and signature are the one-module class's; ``gate`` is ``admission/gate.py`` itself.
_MEMBERS: dict[str, tuple[str, str, str]] = {
    "resolve_spawn_execution": (
        "function",
        "(self, *, parent_session_key='', agent='', conversation_key='', memory_store='',"
        " app='', crew='', target_member=None, _memory_mode='persistent',"
        " _execution_context=None, _record=Ellipsis, _inherited_selection=None)"
        " -> 'ExecutionContext'",
        "gate_execution",
    ),
    "spawn_impl": ("function", "<kept>", "gate"),
    "_memory_pressure_hold_impl": (
        "function",
        "(self, *, floor_gb: 'float | None' = None) -> 'int | None'",
        "gate_pressure",
    ),
    "_pressure_lane_started_impl": (
        "function",
        "(self, parent_session_key: 'str', started_at: 'float') -> 'None'",
        "gate_pressure",
    ),
    "_forget_pressure_clock_impl": (
        "function",
        "(self, agent_id: 'str') -> 'None'",
        "gate_pressure",
    ),
    "_memory_pressure_holds_impl": (
        "function",
        "(self, agent_id: 'str', level: 'int', *, parent_session_key: 'str' = '',"
        " batch_id: 'str' = '', available_gb: 'float | None' = None, relabel: 'bool' = False,"
        " commit_expiry: 'bool' = True) -> 'str'",
        "gate_pressure",
    ),
    "_safe_announce_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo') -> 'None'",
        "gate_reporting",
    ),
    "_record_crew_log_dispatch": (
        "function",
        "(self, info: 'SubagentInfo', *, from_queue: 'bool',"
        " asked: \"'Sequence[Any] | None'\" = None) -> 'None'",
        "gate_reporting",
    ),
    "_record_crew_log_spawn_started": (
        "function",
        "(self, info: 'SubagentInfo') -> 'None'",
        "gate_reporting",
    ),
    "_record_crew_log_spawn_approval_requested": (
        "function",
        "(self, info: 'SubagentInfo', *, approval_id: 'str', reason: 'str')"
        " -> \"'tuple[str, int]'\"",
        "gate_reporting",
    ),
    "_announce_rejection_impl": (
        "function",
        "(self, info: 'SubagentInfo') -> 'SubagentInfo'",
        "gate_reporting",
    ),
}


def _owner(name: str) -> ModuleType:
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))


def _kind(value: object) -> str:
    if inspect.iscoroutinefunction(value):
        return "coroutine"
    if inspect.isfunction(value):
        return "function"
    return "data"


def _signature(value: Any) -> str:
    """The member's signature with the ``self`` annotation removed."""
    signature = inspect.signature(value)
    params = [
        p.replace(annotation=inspect.Parameter.empty) if p.name == "self" else p
        for p in signature.parameters.values()
    ]
    return str(signature.replace(parameters=params))


def _members() -> dict[str, object]:
    return {
        name: value for name, value in vars(_GateMixin).items() if isinstance(value, FunctionType)
    }


# --------------------------------------------------------------------------- #
# The member inventory and the binding.
# --------------------------------------------------------------------------- #


def test_the_kind_reader_tells_a_coroutine_from_a_function() -> None:
    async def stand_in(self: object) -> None:
        return None

    assert (_kind(stand_in), _kind(lambda self: None), _kind(1)) == (
        "coroutine",
        "function",
        "data",
    )


def test_the_member_set_is_the_frozen_inventory() -> None:
    assert sorted(_members()) == sorted(_MEMBERS)


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_signature_and_owner(name: str) -> None:
    kind, signature, owner = _MEMBERS[name]
    value = _members()[name]
    assert _kind(value) == kind
    if owner == "gate":
        assert getattr(value, "__module__") == _GATE
        return
    assert _signature(value) == signature
    # The class attribute IS the owner's function, not a wrapper around it.
    assert value is vars(_owner(owner))[name]


def test_every_owner_holds_a_member_and_sits_beside_the_gate() -> None:
    owning = {owner for _kind, _sig, owner in _MEMBERS.values()} - {"gate"}
    assert owning == set(_OWNERS)
    siblings = {p.stem for p in Path(str(gate_module.__file__)).parent.glob("gate_*.py")}
    assert siblings == set(_OWNERS)


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The top-level functions of *tree* whose first parameter is ``self``."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            if args and args[0].arg == "self":
                found.append(node)
    return found


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = _owner(owner)
    members = _members()
    unbound = [
        node.name
        for node in _self_functions(_tree(module))
        if members.get(node.name) is not vars(module)[node.name]
    ]
    assert unbound == []


# --------------------------------------------------------------------------- #
# Where each bound member's globals come from.
# --------------------------------------------------------------------------- #


def _global_loads(code: CodeType) -> set[str]:
    """Every name *code* (and each function nested in it) reads as a module global."""
    found: set[str] = set()
    stack = [code]
    while stack:
        current = stack.pop()
        for instruction in dis.get_instructions(current):
            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}:
                found.add(str(instruction.argval))
        stack += [const for const in current.co_consts if isinstance(const, CodeType)]
    return found


def _unresolved(function: FunctionType) -> list[str]:
    namespace = function.__globals__
    return sorted(
        name
        for name in _global_loads(function.__code__)
        if name not in namespace and not hasattr(builtins, name)
    )


def test_the_resolution_scan_can_fail() -> None:
    def reads_a_missing_global(self: object) -> object:
        return _a_name_no_module_binds  # type: ignore[name-defined]  # noqa: F821

    assert _unresolved(reads_a_missing_global) == ["_a_name_no_module_binds"]


@pytest.mark.parametrize("name", sorted(n for n in _MEMBERS if n.endswith("_impl")))
def test_an_impl_member_runs_rebound_on_the_subagent_globals(name: str) -> None:
    rebound = vars(SpawnAdmissionCoordinator)[name]
    assert rebound.__globals__ is vars(subagent_module)
    # The rebound copy carries the bound function's own code, so the body that runs is
    # the owner's (or, for ``spawn_impl``, the gate's).
    assert rebound.__code__ is vars(_GateMixin)[name].__code__
    assert _unresolved(rebound) == []


@pytest.mark.parametrize("name", sorted(n for n in _MEMBERS if not n.endswith("_impl")))
def test_a_plain_member_runs_on_its_owner_globals(name: str) -> None:
    function = vars(_GateMixin)[name]
    assert function.__globals__ is vars(_owner(_MEMBERS[name][2]))
    assert _unresolved(function) == []


# --------------------------------------------------------------------------- #
# Constructs that only work inside a class body.
# --------------------------------------------------------------------------- #


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What in *fn* only works inside a class body."""
    found = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            found.append(f"{fn.name}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            found.append(f"{fn.name}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            found.append(f"{fn.name}: {node.attr}")
    return found


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(self):\n    return super().g()\n", True),
        ("def f(self):\n    return __class__\n", True),
        ("def f(self):\n    return self.__secret\n", True),
        ("def f(self):\n    return super(type(self), self).g()\n", False),
        ("def f(self):\n    return self.__dict__, self._private\n", False),
    ],
)
def test_the_class_only_scan_sees_each_shape(source: str, flagged: bool) -> None:
    (fn,) = _self_functions(ast.parse(source))
    assert bool(_class_only_constructs(fn)) is flagged


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(_owner(owner))
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert [c for fn in functions for c in _class_only_constructs(fn)] == []


# --------------------------------------------------------------------------- #
# Owners never import the gate, and only the gate imports an owner.
# --------------------------------------------------------------------------- #


def _runtime_imports(tree: ast.Module) -> set[str]:
    """Dotted modules *tree* imports outside ``if TYPE_CHECKING:``, a relative import
    resolved against the admission package."""
    found: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
                visit(node.orelse)
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    found.update(alias.name for alias in sub.names)
                elif isinstance(sub, ast.ImportFrom):
                    if sub.level:
                        base = _PACKAGE.split(".")[: len(_PACKAGE.split(".")) - sub.level + 1]
                        module = ".".join(base + ([sub.module] if sub.module else []))
                    else:
                        module = sub.module or ""
                    found.add(module)
                    found.update(f"{module}.{alias.name}" for alias in sub.names)

    visit(tree.body)
    return found


def test_the_runtime_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .gate import _GateMixin\n"
        "from . import gate\n"
        "def f(self):\n"
        "    from .gate import _GateMixin\n"
    )
    imports = _runtime_imports(tree)
    assert f"{_PACKAGE}.gate" in imports
    assert f"{_GATE}._GateMixin" in imports


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_imports_the_gate_at_run_time(owner: str) -> None:
    assert _GATE not in _runtime_imports(_tree(_owner(owner)))


def _owner_importers(src: Path) -> list[str]:
    """Every ``src`` module other than the gate that imports a ``gate_*`` owner."""
    owners = {f"{_PACKAGE}.{name}" for name in _OWNERS}
    found = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).with_suffix("")
        module = ".".join(rel.parts)
        if module in owners | {_GATE}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "gate_" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom):
                parent = rel.parts[:-1]
                if node.level:
                    base = list(parent[: len(parent) - node.level + 1])
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                names = {f"{target}.{alias.name}" for alias in node.names}
                if ({target} | names) & owners:
                    found.append(module)
            elif isinstance(node, ast.Import):
                if {alias.name for alias in node.names} & owners:
                    found.append(module)
    return found


def test_the_owner_importer_scan_can_fail(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "subagent_manager" / "admission"
    pkg.mkdir(parents=True)
    (pkg / "pump.py").write_text("from .gate_pressure import f\n", encoding="utf-8")
    (tmp_path / "kiro_crew" / "other.py").write_text(
        "from kiro_crew.subagent_manager.admission import gate_reporting\n", encoding="utf-8"
    )
    assert _owner_importers(tmp_path) == [
        "kiro_crew.other",
        "kiro_crew.subagent_manager.admission.pump",
    ]


def test_nothing_else_in_src_imports_an_owner() -> None:
    assert _owner_importers(_REPO_ROOT / "src") == []


# --------------------------------------------------------------------------- #
# Each owner names the manager fields it writes and reads.
# --------------------------------------------------------------------------- #


def _fields(tree: ast.Module) -> tuple[set[str], set[str]]:
    """``(written, read)`` manager fields: ``self._manager.<f>`` and ``mgr.<f>``."""
    written: set[str] = set()
    read: set[str] = set()
    mutators = {"pop", "add", "discard", "setdefault", "append", "clear", "update", "remove"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        holder = node.value
        on_manager = (isinstance(holder, ast.Name) and holder.id == "mgr") or (
            isinstance(holder, ast.Attribute)
            and holder.attr == "_manager"
            and isinstance(holder.value, ast.Name)
            and holder.value.id == "self"
        )
        if not on_manager:
            continue
        (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(node.attr)
    for node in ast.walk(tree):
        # ``mgr.f[k] = v`` and ``mgr.f.pop(k)`` write ``f`` too.
        target = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            target = node.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in mutators
        ):
            target = node.func.value
        if isinstance(target, ast.Attribute) and target.attr in read:
            written.add(target.attr)
    return written, read - written


def test_the_field_scan_sees_writes_and_reads() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    mgr = self._manager\n"
        "    mgr._a = 1\n"
        "    mgr._b[1] = 2\n"
        "    mgr._c.pop(1)\n"
        "    return self._manager._d, getattr(self._manager, '_e', None)\n"
    )
    assert _fields(tree) == ({"_a", "_b", "_c"}, {"_d"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)(?:\. [A-Z]|\.$)", " ".join(doc.split()))
    assert match, f"no {label!r} sentence"
    return set(re.findall(r"``(\w+)``", match.group(1)))


@pytest.mark.parametrize("owner", _OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = _owner(owner)
    written, read = _fields(_tree(module))
    doc = module.__doc__ or ""
    assert _listed(doc, "Writes") == written
    # The read list names what is read without being written, plus fields reached
    # through ``getattr`` (which the scan cannot see) -- never fewer than the scan finds.
    assert read <= _listed(doc, "Reads")


# --------------------------------------------------------------------------- #
# No name a test patches through the gate module is read by an owner.
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/`` and the builtin apps' test modules and conftests."""
    files = [_REPO_ROOT / "conftest.py", *sorted(_TEST_ROOT.rglob("*.py"))]
    builtins_root = _REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins"
    for path in sorted(builtins_root.rglob("*.py")):
        rel = path.relative_to(builtins_root).as_posix()
        if "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py":
            files.append(path)
    return files


def _patched_through_the_gate(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Every spelling of the gate module, and every alias a test binds to it, says "gate".
        if "admission" not in text or "gate" not in text:
            continue
        try:
            found = patched_names(text, {_GATE: _GATE})
        except SyntaxError:
            continue
        names |= found.get(_GATE, set())
    return names


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    return frozenset(_patched_through_the_gate(_test_tree_files()))


def _owner_reads() -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for owner in _OWNERS:
        module = _owner(owner)
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        reads[module.__name__] = _global_loads(compile(source, str(module.__file__), "exec"))
    return reads


def _reach_gaps(patched: set[str], reads: dict[str, set[str]]) -> list[str]:
    return sorted(f"{owner}.{name}" for owner, names in reads.items() for name in patched & names)


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    files = _test_tree_files()
    assert Path(__file__).resolve() in files
    assert _REPO_ROOT / "conftest.py" in files
    assert any("/apps/builtins/" in path.as_posix() for path in files)
    assert len(files) > 1000


def test_no_name_patched_through_the_gate_is_read_by_an_owner() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _owner_reads()) == []


def test_the_reach_scan_can_fail(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from kiro_crew.subagent_manager.admission import gate as gate_mod\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(gate_mod, '_SPAWN_APPROVAL_TOOL', 'x')\n"
        "def test_y():\n"
        "    from unittest import mock\n"
        "    with mock.patch(\n"
        "        'kiro_crew.subagent_manager.admission.gate.read_memory_pressure_level'\n"
        "    ):\n"
        "        pass\n",
        encoding="utf-8",
    )
    patched = _patched_through_the_gate([probe])
    assert patched == {"_SPAWN_APPROVAL_TOOL", "read_memory_pressure_level"}
    assert _reach_gaps(patched, _owner_reads()) == [
        f"{_PACKAGE}.gate_pressure.read_memory_pressure_level",
        f"{_PACKAGE}.gate_reporting._SPAWN_APPROVAL_TOOL",
    ]


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    rel = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert rel in leaf_test_scope.corpus_gates(_REPO_ROOT)
