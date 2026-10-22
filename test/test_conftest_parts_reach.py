"""A name a test patches on a conftest is read only in that conftest, where the patch lands.

Both conftests keep most of their definitions in plain modules beside the suite:
``test/conftest_floor_*.py`` for the rootdir ``conftest.py`` and ``test/conftest_*.py``
for ``test/conftest.py``. Each conftest binds those names back into its own namespace
with a ``from conftest_* import`` block. A test that patches a name on a conftest module
therefore rebinds the conftest's global and nothing else: a function defined in a part
reads the part's own binding and never sees the patch.

The guard resolves the patches the test trees make on a conftest module it can name: a
by-path load of ``conftest.py`` (the ``_root`` helpers), a ``get_plugin`` lookup of
``<repo>/conftest.py``, ``test/conftest.py`` found among the loaded plugins, ``import
conftest``, and a helper's parameter that every caller passes one of these. It fails when
a function that reads a patched name lives in a part, and it fails closed where it cannot
follow: a loader whose path it cannot work out, a patched name that is not a constant,
and, in a file that reaches a conftest, a patch call on a parameter or local it cannot
resolve. A plugin written as a string is not parsed: each one that patches a conftest is
a row in :data:`_STRING_PLUGIN_PATCHES`, and an unlisted one fails.

A patch whose test runs only the conftest's own reader may leave a moved reader
unpatched; each such case is one row in :data:`_MOVED_READERS_THE_PATCH_SKIPS`, naming
the patching function, the reader and why it is never run. The detector runs on a
synthetic tree first, each shape both ways, so a shape it stopped recognising reddens
there.
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import importlib
import posixpath
import re
import symtable
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
import source_corpus

#: One worker runs the module, so the corpus scan behind ``live_scan`` happens once.
pytestmark = pytest.mark.xdist_group(name="tree_scan_conftest_parts_reach")

#: The two conftests, repo-relative.
_CONFTESTS = ("conftest.py", "test/conftest.py")

_LOOP_JOIN = "test/conftest_floor_restores.py::_join_test_loop_executor"

#: Patches whose test exercises only the conftest's own reader of the patched name, as
#: (patching file, patching function, conftest, name) -> {moved reader: why it is not run}.
_MOVED_READERS_THE_PATCH_SKIPS: dict[tuple[str, str, str, str], dict[str, str]] = {
    (
        "test/test_host_isolation_floor.py",
        "TestNoConsentWorkerOutlivesItsTest.test_a_worker_past_the_bound_is_reported_not_waited_for_forever",
        "conftest.py",
        "_EXECUTOR_JOIN_SECS",
    ): {
        _LOOP_JOIN: (
            "the test shortens the bound to drive _join_telemetry_consent_worker, which "
            "stays in conftest.py and reads the conftest's binding; it never runs the "
            "per-test event-loop join, whose own tests use the full bound"
        ),
    },
    (
        "test/test_testing_determinism_helpers.py",
        "TestDefaultTimeout.test_the_runner_imports_wait_for_a_test_that_imports_it_late",
        "conftest.py",
        "importlib",
    ): {
        f"test/conftest_floor_{reader}": (
            "the test swaps importlib on the live plugin to watch _publish_runner_timeout's "
            "late import, which stays in conftest.py; this reader runs only while fixtures "
            "set up, which ends before the test body installs the stand-in"
        )
        for reader in (
            "home.py::_agent_spec_seam_modules",
            "home.py::_isolate_kiro_sessions_dir",
            "seams.py::_isolate_launchd_paths",
            "seams.py::_isolate_sandbox_mount_source_roots",
            "seams.py::_pin_agent_state_sidecar",
            "seams.py::_pin_kill_and_reap_group_probe",
        )
    },
    (
        "test/test_xdist_escaped_failure_guard.py",
        "test_a_non_failed_outcome_is_left_untouched",
        "conftest.py",
        "time",
    ): {
        _LOOP_JOIN: (
            "the test freezes the clock on a private by-path copy to drive "
            "pytest_runtest_protocol, which stays in conftest.py; it never runs the "
            "per-test event-loop join"
        ),
    },
    (
        "test/test_xdist_escaped_failure_guard.py",
        "_drive_protocol",
        "conftest.py",
        "time",
    ): {
        _LOOP_JOIN: (
            "the helper freezes the clock on a private by-path copy to drive "
            "pytest_runtest_protocol, which stays in conftest.py; it never runs the "
            "per-test event-loop join"
        ),
    },
}

#: Plugins written as a string that patch a conftest they import, which the scan does
#: not parse, as (file, module-level name holding the text, conftest, patched name) ->
#: {moved reader of that name: why the patch does not need to reach it}.
_STRING_PLUGIN_PATCHES: dict[tuple[str, str, str, str], dict[str, str]] = {
    ("test/test_xdist_escaped_failure_guard.py", "_PLUGIN", "conftest.py", "time"): {
        _LOOP_JOIN: (
            "the inner session (-p conftest, the rootdir file) runs tests that request no "
            "event_loop fixture, so the per-test event-loop join returns before it reads "
            "the clock"
        ),
    },
}

_PATH_CALLS = frozenset({"Path", "PurePath", "PosixPath", "str", "fspath"})
#: ``os.path`` functions that keep the path a string names (``dirname`` drops a leaf).
_SAME_PATH_CALLS = frozenset({"abspath", "realpath", "normpath"})
_CONFTEST_LITERAL = re.compile(r"""conftest\.py["']|["']conftest\.|^\s*import conftest\b""", re.M)
_STRING_PLUGIN = re.compile(r"^\s*(?:import conftest\b|from conftest import)", re.M)
_SCOPE_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_BUILTINS = frozenset(dir(builtins))
_UNRESOLVED: tuple[str, frozenset[str]] = ("unresolved", frozenset())
_OTHER: tuple[str, frozenset[str]] = ("other", frozenset())


@dataclasses.dataclass(frozen=True)
class PatchSite:
    """One patch of *name* on a conftest module, at *path*:*line* inside *function*."""

    path: str
    line: int
    function: str
    conftest: str
    name: str


@dataclasses.dataclass
class Scan:
    """What one pass over the test trees found."""

    sites: list[PatchSite]
    #: ``path:line function: target`` for patch calls the scan could not resolve.
    unresolved: list[str]


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


class _Marker(ast.expr):
    """A binding whose kind the scan knows but whose value it does not track."""


class _FixtureParam(_Marker):
    """A parameter that receives the fixture of the same name, defined in this module."""

    def __init__(self, fixture: str) -> None:
        super().__init__()
        self.fixture = fixture


class _Param(_Marker):
    """A parameter, resolved from the call sites of *function* when it is module-level."""

    def __init__(self, function: str | None, index: int | None, name: str) -> None:
        super().__init__()
        self.function, self.index, self.name = function, index, name


class _PluginLoop(_Marker):
    """The loop variable of ``for plugin in <...>.get_plugins()``."""


class _ImportedConftest(_Marker):
    """A name bound by ``import conftest [as name]``."""


class _Unknown(_Marker):
    """A local bound by a statement the scan does not follow (a loop, ``with``, unpacking)."""


class _Other(_Marker):
    """A binding that is certainly not a conftest module."""


class _Unpacked(_Marker):
    """Element *index* of the tuple a call returns, unpacked into a local."""

    def __init__(self, call: ast.Call, index: int, arity: int) -> None:
        super().__init__()
        self.call, self.index, self.arity = call, index, arity


class _Module:
    """One parsed corpus file, with just enough name resolution to follow a handle.

    A *handle* is an expression that evaluates to a conftest module: a by-path load
    (``module_from_spec`` of a spec whose location is a conftest), a ``get_plugin``
    lookup of a conftest's path, a call to a function that returns one (including a
    ``for plugin in ...get_plugins()`` search compared against a conftest's path), a
    fixture that returns one, ``import conftest``, a parameter every caller passes one
    to, or a name bound to any of these, here or in a module this one imports it from.
    """

    def __init__(self, corpus: _Corpus, path: Path) -> None:
        self.corpus, self.path = corpus, path
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = _rel(corpus.root, path)
        #: ``import conftest`` from ``test/`` binds ``test/conftest.py``; elsewhere the rootdir file.
        self.imported_conftest = _CONFTESTS[1] if rel.startswith("test/") else _CONFTESTS[0]
        self.assigns: dict[str, ast.expr] = {}
        self.imports: dict[str, tuple[str, str]] = {}
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.fixtures: dict[str, str] = {}
        self.module_names = _bound_names(self.tree)
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assigns[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    self.assigns[node.target.id] = node.value
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "conftest":
                        self.assigns[alias.asname or alias.name] = _ImportedConftest()
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
                fixture = _fixture_name(node)
                if fixture:
                    self.fixtures[fixture] = node.name
        self._factories: dict[str, frozenset[str]] = {}
        self._params: dict[tuple[str, int | None, str], tuple[str, frozenset[str]]] = {}
        self._unpacked: dict[tuple[int, int], tuple[str, frozenset[str]]] = {}
        self._envs: dict[int, dict[str, ast.expr]] = {}

    def env_of(self, scope: ast.AST) -> dict[str, ast.expr]:
        if id(scope) not in self._envs:
            self._envs[id(scope)] = {} if isinstance(scope, ast.Module) else scope_env(self, scope)
        return self._envs[id(scope)]

    # -- paths -------------------------------------------------------------------

    def path_of(self, node: ast.expr, env: dict[str, ast.expr], depth: int = 0) -> str | None:
        """*node* as a posix path string, or None when it is not a path this can follow."""
        if depth > 40 or isinstance(node, _Marker):
            return None
        nxt = depth + 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return self.path.resolve().as_posix()
            if node.id in env:
                return self.path_of(env[node.id], _without(env, node.id), nxt)
            if node.id in self.assigns:
                return self.path_of(self.assigns[node.id], {}, nxt)
            if node.id in self.imports:
                other = self.corpus.module_named(self.imports[node.id][0], self.path.parent)
                if other is not None:
                    return other.path_of(ast.Name(self.imports[node.id][1]), {}, nxt)
            return None
        if isinstance(node, ast.Attribute):
            if node.attr in ("rootpath", "rootdir"):
                return self.corpus.root.resolve().as_posix()
            if node.attr == "parent":
                base = self.path_of(node.value, env, nxt)
                return posixpath.dirname(base) if base else None
            return None
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "parents"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)
            ):
                base = self.path_of(value.value, env, nxt)
                for _ in range(node.slice.value + 1):
                    base = posixpath.dirname(base) if base else None
                return base
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.path_of(node.left, env, nxt), self.path_of(node.right, env, nxt)
            return posixpath.normpath(posixpath.join(left, right)) if left and right else None
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "repo_root":
                return self.corpus.root.resolve().as_posix()
            if name in _PATH_CALLS and len(node.args) == 1:
                return self.path_of(node.args[0], env, nxt)
            if name in _SAME_PATH_CALLS and len(node.args) == 1:
                base = self.path_of(node.args[0], env, nxt)
                return posixpath.normpath(base) if base else None
            if name == "dirname" and len(node.args) == 1:
                base = self.path_of(node.args[0], env, nxt)
                return posixpath.dirname(base) if base else None
            if isinstance(node.func, ast.Attribute):
                if name in ("resolve", "absolute") and not node.args:
                    return self.path_of(node.func.value, env, nxt)
                if name == "with_name" and len(node.args) == 1:
                    base = self.path_of(node.func.value, env, nxt)
                    leaf = self.path_of(node.args[0], env, nxt)
                    return posixpath.join(posixpath.dirname(base), leaf) if base and leaf else None
                if name in ("joinpath", "join"):
                    parts = [self.path_of(arg, env, nxt) for arg in node.args]
                    if name == "joinpath":
                        parts.insert(0, self.path_of(node.func.value, env, nxt))
                    if parts and all(parts):
                        return posixpath.normpath(posixpath.join(*parts))
        return None

    def conftest_at(self, node: ast.expr, env: dict[str, ast.expr]) -> str | None:
        path = self.path_of(node, env)
        if path is None:
            return None
        return self.corpus.conftest_paths.get(posixpath.normpath(path))

    # -- handles -----------------------------------------------------------------

    def handle_of(self, node: ast.expr, env: dict[str, ast.expr], depth: int = 0) -> frozenset[str]:
        """The conftest(s) *node* evaluates to, or an empty set."""
        if depth > 40:
            return frozenset()
        nxt = depth + 1
        if isinstance(node, _ImportedConftest):
            return frozenset({self.imported_conftest})
        if isinstance(node, _FixtureParam):
            return self.factory(self.fixtures[node.fixture])
        if isinstance(node, _Param):
            status, found = self.param_handles(node)
            return found if status == "handle" else frozenset()
        if isinstance(node, _Unpacked):
            status, found = self.unpacked(node)
            return found if status == "handle" else frozenset()
        if isinstance(node, _Marker):
            return frozenset()
        if isinstance(node, ast.Name):
            if node.id in env:
                return self.handle_of(env[node.id], _without(env, node.id), nxt)
            if node.id in self.assigns:
                return self.handle_of(self.assigns[node.id], {}, nxt)
            if node.id in self.imports:
                other = self.corpus.module_named(self.imports[node.id][0], self.path.parent)
                if other is not None:
                    return other.handle_of(ast.Name(self.imports[node.id][1]), {}, nxt)
            return frozenset()
        if not isinstance(node, ast.Call):
            return frozenset()
        name = _call_name(node)
        if name == "module_from_spec" and node.args:
            return self._spec_target(node.args[0], env, nxt)
        if name == "get_plugin" and node.args:
            found = self.conftest_at(node.args[0], env)
            return frozenset({found}) if found else frozenset()
        if isinstance(node.func, ast.Name):
            if node.func.id in self.functions:
                return self.factory(node.func.id)
            if node.func.id in self.imports:
                home, attr = self.imports[node.func.id]
                other = self.corpus.module_named(home, self.path.parent)
                if other is not None and attr in other.functions:
                    return other.factory(attr)
        return frozenset()

    def classify(
        self, node: ast.expr, env: dict[str, ast.expr], depth: int = 0
    ) -> tuple[str, frozenset[str]]:
        """("handle", conftests), ("other", {}) or ("unresolved", {}) for a patch target."""
        found = self.handle_of(node, env)
        if found:
            return "handle", found
        if depth > 40:
            return _UNRESOLVED
        if isinstance(node, _Param):
            return self.param_handles(node)
        if isinstance(node, _Unpacked):
            return self.unpacked(node)
        if isinstance(node, (_Unknown, _PluginLoop)):
            return _UNRESOLVED
        if isinstance(node, _Marker):
            return _OTHER
        if isinstance(node, ast.Name):
            if node.id in env:
                return self.classify(env[node.id], _without(env, node.id), depth + 1)
            if node.id in self.assigns:
                return self.classify(self.assigns[node.id], {}, depth + 1)
            if node.id in self.module_names or node.id in _BUILTINS:
                return _OTHER
            return _UNRESOLVED
        return _OTHER

    def param_handles(self, param: _Param) -> tuple[str, frozenset[str]]:
        """A module-level function's parameter, judged from every call site in this file."""
        if param.function is None:
            return _UNRESOLVED
        key = (param.function, param.index, param.name)
        if key in self._params:
            return self._params[key]
        self._params[key] = _UNRESOLVED  # a recursive call resolves to nothing
        verdicts = []
        for _qualname, scope, node in _scoped_nodes(self.tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == param.function
            ):
                continue
            argument = _argument(node, param)
            if argument is None:
                verdicts.append(_OTHER)  # the parameter's default
            elif isinstance(argument, _Unknown):
                verdicts.append(_UNRESOLVED)
            else:
                verdicts.append(self.classify(argument, self.env_of(scope)))
        if verdicts and all(v == verdicts[0] for v in verdicts) and verdicts[0] != _UNRESOLVED:
            self._params[key] = verdicts[0]
        return self._params[key]

    def unpacked(self, marker: _Unpacked) -> tuple[str, frozenset[str]]:
        """One element of the tuple a module function or a ``self.``/``cls.`` method returns."""
        key = (id(marker.call), marker.index)
        if key in self._unpacked:
            return self._unpacked[key]
        self._unpacked[key] = _UNRESOLVED  # a recursive call resolves to nothing
        callee = self._callee(marker.call)
        if callee is None:
            return _UNRESOLVED
        verdicts = []
        for sub in _own_nodes(callee):
            if isinstance(sub, ast.Return):
                value = sub.value
                if isinstance(value, ast.Tuple) and len(value.elts) == marker.arity:
                    verdicts.append(self.classify(value.elts[marker.index], self.env_of(callee)))
                else:
                    verdicts.append(_UNRESOLVED)
        if verdicts and all(v == verdicts[0] for v in verdicts) and verdicts[0] != _UNRESOLVED:
            self._unpacked[key] = verdicts[0]
        return self._unpacked[key]

    def _callee(self, call: ast.Call) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """The function *call* runs: a module-level one, or the one method of that name."""
        if isinstance(call.func, ast.Name):
            return self.functions.get(call.func.id)
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
        ):
            methods = [
                node
                for owner in ast.walk(self.tree)
                if isinstance(owner, ast.ClassDef)
                for node in owner.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == func.attr
            ]
            return methods[0] if len(methods) == 1 else None
        return None

    def _spec_target(self, node: ast.expr, env: dict[str, ast.expr], depth: int) -> frozenset[str]:
        if depth > 40 or isinstance(node, _Marker):
            return frozenset()
        if isinstance(node, ast.Name) and node.id in env:
            return self._spec_target(env[node.id], _without(env, node.id), depth + 1)
        if isinstance(node, ast.Name) and node.id in self.assigns:
            return self._spec_target(self.assigns[node.id], {}, depth + 1)
        if isinstance(node, ast.Call) and _call_name(node) == "spec_from_file_location":
            location = node.args[1] if len(node.args) > 1 else None
            for keyword in node.keywords:
                if keyword.arg == "location":
                    location = keyword.value
            found = self.conftest_at(location, env) if location is not None else None
            return frozenset({found}) if found else frozenset()
        return frozenset()

    def factory(self, function: str) -> frozenset[str]:
        """The conftest(s) the module-level function *function* returns."""
        if function in self._factories:
            return self._factories[function]
        self._factories[function] = frozenset()  # a recursive call resolves to nothing
        node = self.functions[function]
        env = self.env_of(node)
        found: set[str] = set()
        for sub in _own_nodes(node):
            if isinstance(sub, (ast.Return, ast.Yield)) and sub.value is not None:
                value = sub.value
                if isinstance(value, ast.Name) and isinstance(env.get(value.id), _PluginLoop):
                    found |= self._plugin_search(node, env)
                else:
                    found |= self.handle_of(value, env)
        self._factories[function] = frozenset(found)
        return self._factories[function]

    def _plugin_search(self, node: ast.AST, env: dict[str, ast.expr]) -> set[str]:
        """``for plugin in pluginmanager.get_plugins(): ... return plugin`` matched on a path."""
        found: set[str] = set()
        for sub in _own_nodes(node):
            if isinstance(sub, ast.Compare):
                for operand in (sub.left, *sub.comparators):
                    hit = self.conftest_at(operand, env)
                    if hit:
                        found.add(hit)
        return found


def _without(env: dict[str, ast.expr], name: str) -> dict[str, ast.expr]:
    return {key: value for key, value in env.items() if key != name}


def _argument(call: ast.Call, param: _Param) -> ast.expr | None:
    """The expression *call* passes for *param*: None means its default, unknown a splat."""
    if param.index is not None:
        if any(isinstance(arg, ast.Starred) for arg in call.args[: param.index + 1]):
            return _Unknown()
        if param.index < len(call.args):
            return call.args[param.index]
    for keyword in call.keywords:
        if keyword.arg == param.name:
            return keyword.value
        if keyword.arg is None:
            return _Unknown()
    return None


def _fixture_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        if (isinstance(target, ast.Attribute) and target.attr == "fixture") or (
            isinstance(target, ast.Name) and target.id == "fixture"
        ):
            for keyword in call.keywords if call else ():
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
            return node.name
    return None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Every node inside *node* that is not inside a nested function or class."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        sub = stack.pop()
        yield sub
        if not isinstance(sub, (*_SCOPE_KINDS, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(sub))


def _bound_names(node: ast.AST) -> set[str]:
    """Every name a statement directly inside *node*'s own scope binds."""
    names: set[str] = set()
    for sub in _own_nodes(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in sub.names}
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(sub.name)
    return names


def scope_env(module: _Module, node: ast.AST) -> dict[str, ast.expr]:
    """The local bindings of one function: parameters, assignments, imports, plugin loops.

    A local the scan does not follow is still recorded, as :class:`_Unknown`, so a patch
    through it is reported rather than taken for a name from the module.
    """
    env: dict[str, ast.expr] = {name: _Unknown() for name in _bound_names(node)}
    for sub in _own_nodes(node):
        if isinstance(sub, (ast.Global, ast.Nonlocal)):
            for name in sub.names:
                env.pop(name, None)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name if module.functions.get(node.name) is node else None
        for index, arg in enumerate((*node.args.posonlyargs, *node.args.args)):
            env[arg.arg] = _param(module, function, index, arg.arg)
        for arg in node.args.kwonlyargs:
            env[arg.arg] = _param(module, function, None, arg.arg)
        for arg in (node.args.vararg, node.args.kwarg):
            if arg is not None:
                env[arg.arg] = _Unknown()
    for sub in _own_nodes(node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = sub.value
                elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(sub.value, ast.Call):
                    if not any(isinstance(elt, ast.Starred) for elt in target.elts):
                        for index, elt in enumerate(target.elts):
                            if isinstance(elt, ast.Name):
                                env[elt.id] = _Unpacked(sub.value, index, len(target.elts))
        elif isinstance(sub, ast.For) and isinstance(sub.target, ast.Name):
            if isinstance(sub.iter, ast.Call) and _call_name(sub.iter) == "get_plugins":
                env[sub.target.id] = _PluginLoop()
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                bound = alias.asname or alias.name.split(".")[0]
                env[bound] = _ImportedConftest() if alias.name == "conftest" else _Other()
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                env[alias.asname or alias.name] = _Other()
    return env


def _param(module: _Module, function: str | None, index: int | None, name: str) -> ast.expr:
    if name in module.fixtures:
        return _FixtureParam(name)
    if name in ("self", "cls"):
        return _Other()
    return _Param(function, index, name)


class _Corpus:
    """The test-tree files, parsed on demand, and where the two conftests live."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.conftest_paths = {(root / rel).resolve().as_posix(): rel for rel in _CONFTESTS}
        self._modules: dict[Path, _Module | None] = {}

    def module(self, path: Path) -> _Module | None:
        path = path.resolve()
        if path not in self._modules:
            try:
                self._modules[path] = _Module(self, path)
            except (OSError, SyntaxError, UnicodeDecodeError):
                self._modules[path] = None
        return self._modules[path]

    def module_named(self, dotted: str, near: Path) -> _Module | None:
        """A flat module imported by bare name: beside the importer, or under ``test/``."""
        if "." in dotted:
            return None
        for folder in (near, self.root / "test"):
            candidate = folder / f"{dotted}.py"
            if candidate.is_file():
                return self.module(candidate)
        return None


def patch_sites(root: Path, paths: Iterable[Path], corpus: _Corpus | None = None) -> Scan:
    """Every patch the files at *paths* make on either conftest module, and what is unclear.

    A patch call whose target the scan cannot resolve is reported as unresolved in any
    file that reaches a conftest (builds, imports or receives a handle), never dropped.
    """
    corpus = corpus or _Corpus(root)
    found = Scan([], [])
    for path in paths:
        module = corpus.module(path)
        if module is None:
            continue
        rel = _rel(root, path)
        unclear: list[str] = []
        reaches = _exports_a_handle(module) or _imports_a_handle(module)
        for qualname, scope, node in _scoped_nodes(module.tree):
            env = module.env_of(scope)
            if not reaches and isinstance(node, ast.Call) and module.handle_of(node, env):
                reaches = True
            if isinstance(node, ast.Import) and any(a.name == "conftest" for a in node.names):
                reaches = True
            for target, names in _patches(node):
                if isinstance(target, _Dotted):
                    hits = {module.imported_conftest} if target.module == "conftest" else set()
                    status = "handle" if hits else "other"
                else:
                    status, conftests = module.classify(target, env)
                    hits = set(conftests)
                if status == "unresolved" and isinstance(node, ast.Call):
                    unclear.append(f"{rel}:{node.lineno} {qualname}: {ast.unparse(target)}")
                if hits:
                    reaches = True
                loops = _loop_constants(scope) if hits else {}
                for conftest in sorted(hits):
                    for name in _resolve_names(names, loops):
                        line = getattr(node, "lineno", 0)
                        found.sites.append(PatchSite(rel, line, qualname, conftest, name))
        if reaches:
            found.unresolved += unclear
    return found


def _scoped_nodes(tree: ast.Module) -> Iterator[tuple[str, ast.AST, ast.AST]]:
    """(qualified name of the enclosing function, that function, node) for every node."""
    stack: list[tuple[str, str, ast.AST, ast.AST]] = [("", "<module>", tree, tree)]
    while stack:
        prefix, qualname, scope, node = stack.pop()
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{sub.name}"
                stack.append((f"{name}.", name, sub, sub))
            elif isinstance(sub, ast.ClassDef):
                stack.append((f"{prefix}{sub.name}.", qualname, scope, sub))
            else:
                yield qualname, scope, sub
                stack.append((prefix, qualname, scope, sub))


def _loop_constants(scope: ast.AST) -> dict[str, tuple[str, ...]]:
    """``for name in ("a", "b"):`` loop variables, so a patch by that name is resolved."""
    loops: dict[str, tuple[str, ...]] = {}
    for sub in _own_nodes(scope):
        if isinstance(sub, ast.For) and isinstance(sub.target, ast.Name):
            if isinstance(sub.iter, (ast.Tuple, ast.List, ast.Set)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in sub.iter.elts
            ):
                loops[sub.target.id] = tuple(e.value for e in sub.iter.elts)
    return loops


class _Dotted(ast.expr):
    """A ``"module.name"`` patch target string."""

    def __init__(self, module: str) -> None:
        super().__init__()
        self.module = module


def _patches(node: ast.AST) -> Iterator[tuple[ast.expr, tuple[ast.expr | str, ...]]]:
    """(patched object, patched names or the nodes naming them) for one patch-shaped node."""
    if isinstance(node, ast.Call):
        name = _call_name(node)
        first = node.args[0] if node.args else None
        if (
            name in ("setattr", "delattr", "patch")
            and isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and "." in first.value
        ):
            module, _, attr = first.value.rpartition(".")
            yield _Dotted(module), (attr,)
        elif name in ("setattr", "delattr", "object") and len(node.args) >= 2:
            yield node.args[0], (node.args[1],)
        elif name == "multiple" and node.args:
            yield node.args[0], tuple(k.arg for k in node.keywords if k.arg)
        elif name == "setitem" and len(node.args) >= 2:
            holder = node.args[0]
            if isinstance(holder, ast.Attribute) and holder.attr == "__dict__":
                yield holder.value, (node.args[1],)
            elif isinstance(holder, ast.Call) and _call_name(holder) == "vars" and holder.args:
                yield holder.args[0], (node.args[1],)
    elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
        targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                yield target.value, (target.attr,)


def _resolve_names(
    names: tuple[ast.expr | str, ...], loops: dict[str, tuple[str, ...]]
) -> Iterator[str]:
    for name in names:
        if isinstance(name, str):
            yield name
        elif isinstance(name, ast.Constant) and isinstance(name.value, str):
            yield name.value
        elif isinstance(name, ast.Name):
            # A name the scan cannot resolve is reported as itself, never dropped.
            yield from loops.get(name.id, (f"<unresolved {name.id}>",))
        else:
            yield f"<unresolved {ast.unparse(name)}>"


def conftest_family(root: Path, conftest: str) -> tuple[str, ...]:
    """*conftest* and every part it binds names from, directly or through another part."""
    family = [conftest]
    for rel in family:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                part = f"test/{node.module}.py"
                if node.module.startswith("conftest_") and (root / part).is_file():
                    if part not in family:
                        family.append(part)
    return tuple(family)


def global_readers(root: Path, rel: str) -> dict[str, set[str]]:
    """name -> the functions in *rel* that read it as a module global, by qualified name."""
    table = symtable.symtable((root / rel).read_text(encoding="utf-8"), rel, "exec")
    readers: dict[str, set[str]] = {}

    def walk(scope: symtable.SymbolTable, qualname: str) -> None:
        for child in scope.get_children():
            name = child.get_name()
            inline = name in ("listcomp", "setcomp", "dictcomp", "genexpr")
            child_qualname = qualname if inline else f"{qualname}.{name}".lstrip(".")
            if child.get_type() == "function" or inline:
                for symbol in child.get_symbols():
                    if symbol.is_global() and symbol.is_referenced():
                        readers.setdefault(symbol.get_name(), set()).add(child_qualname)
            walk(child, child_qualname)

    walk(table, "")
    return readers


def moved_readers(root: Path, sites: Iterable[PatchSite]) -> dict[PatchSite, set[str]]:
    """Each patch site whose name a function in a part also reads, with those readers."""
    families = {conftest: conftest_family(root, conftest) for conftest in _CONFTESTS}
    readers = {rel: global_readers(root, rel) for family in families.values() for rel in family}
    found: dict[PatchSite, set[str]] = {}
    for site in sites:
        for rel in families[site.conftest][1:]:
            for qualname in readers[rel].get(site.name, ()):
                found.setdefault(site, set()).add(f"{rel}::{qualname}")
    return found


def _test_tree_files(root: Path) -> Iterator[Path]:
    """The files pytest collects tests or fixtures from, other than the conftest families."""
    test_root = root / "test"
    for path in source_corpus.repo_files_named(".py") if root == _REPO else root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if not (
            path.is_relative_to(test_root)
            or (rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel)
        ):
            continue
        if rel in _CONFTESTS or path.name.startswith("conftest_"):
            continue
        yield path


def _exports_a_handle(module: _Module) -> bool:
    return any(module.handle_of(ast.Name(name), {}) for name in module.assigns) or any(
        module.factory(name) for name in module.functions
    )


def _imports_a_handle(module: _Module) -> bool:
    """Whether a name this module imports from the corpus is a conftest handle or factory."""
    for name in module.imports:
        call = ast.Call(func=ast.Name(name), args=[], keywords=[])
        if module.handle_of(ast.Name(name), {}) or module.handle_of(call, {}):
            return True
    return False


def scan(root: Path) -> Scan:
    """:func:`patch_sites` over every test-tree file that can reach a conftest.

    Only a quoted ``conftest.py``, a ``"conftest.<name>"`` target or ``import conftest``
    can name a conftest, and only a module importing from a helper that exports a
    conftest handle can receive one without spelling it, so those files are parsed and
    nothing else; the handle resolution decides which of them patch a conftest, and
    :func:`unresolved_loaders` reports every loader the path evaluator cannot follow.
    """
    corpus = _Corpus(root)
    files = list(_test_tree_files(root))
    candidates = [path for path in files if _CONFTEST_LITERAL.search(_text(path))]
    stems = set()
    for path in candidates:
        module = corpus.module(path)
        if not path.name.startswith("test_") and module is not None and _exports_a_handle(module):
            stems.add(path.stem)
    chosen = set(candidates)
    importers = [p for p in files if p not in chosen and any(s in _text(p) for s in stems)]
    return patch_sites(root, [*candidates, *importers], corpus)


def corpus_sites(root: Path) -> list[PatchSite]:
    return scan(root).sites


def unresolved_loaders(root: Path) -> list[str]:
    """Loader calls that name a conftest whose path the scan cannot work out.

    A by-path load or plugin lookup the path evaluator cannot follow would drop that
    file's patches from the guard without a word, so each one is reported instead.
    """
    corpus = _Corpus(root)
    unresolved = []
    for path in _test_tree_files(root):
        text = _text(path)
        if "conftest" not in text or not (
            "spec_from_file_location" in text or "get_plugin" in text
        ):
            continue
        module = corpus.module(path)
        if module is None:
            continue
        for _qualname, scope, sub in _scoped_nodes(module.tree):
            if not isinstance(sub, ast.Call):
                continue
            name = _call_name(sub)
            if name == "spec_from_file_location" and len(sub.args) > 1:
                location: ast.expr = sub.args[1]
            elif name == "get_plugin" and sub.args:
                location = sub.args[0]
            else:
                continue
            if "conftest" in ast.unparse(location) or _names_conftest(module, location):
                if module.path_of(location, module.env_of(scope)) is None:
                    unresolved.append(f"{_rel(root, path)}:{sub.lineno}")
    return unresolved


def _names_conftest(module: _Module, node: ast.expr) -> bool:
    """Whether *node* reads a module constant whose value spells ``conftest``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in module.assigns:
            if "conftest" in ast.unparse(module.assigns[sub.id]):
                return True
    return False


def string_plugins(root: Path) -> dict[tuple[str, str], str]:
    """(file, module-level name) -> text of each string plugin that imports a conftest
    and calls ``setattr``: code the scan does not parse, so it is listed instead."""
    found: dict[tuple[str, str], str] = {}
    corpus = _Corpus(root)
    for path in _test_tree_files(root):
        text = _text(path)
        if not (_STRING_PLUGIN.search(text) and "setattr(" in text):
            continue
        module = corpus.module(path)
        if module is None:
            continue
        for node in module.tree.body:
            if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
                continue
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if _STRING_PLUGIN.search(sub.value) and "setattr(" in sub.value:
                        found[(_rel(root, path), node.targets[0].id)] = sub.value
    return found


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


_REPO = source_corpus.repo_root()
_TEST_DIR = source_corpus.repo_root() / "test"
_THIS_FILE = Path(__file__).resolve()


@pytest.fixture(scope="module")
def live_scan() -> Scan:
    return scan(_REPO)


def test_every_name_patched_on_a_conftest_is_read_only_in_that_conftest(live_scan: Scan) -> None:
    found = moved_readers(_REPO, live_scan.sites)
    unexplained = {}
    for site, readers in found.items():
        allowed = _MOVED_READERS_THE_PATCH_SKIPS.get(
            (site.path, site.function, site.conftest, site.name), {}
        )
        extra = sorted(readers - set(allowed))
        if extra:
            unexplained[f"{site.path}:{site.line} {site.conftest}.{site.name}"] = extra
    assert not unexplained, (
        "A test patches these names on a conftest module, but a function that reads them "
        "lives in a part, which reads its own binding and never sees the patch. Keep the "
        "reader in the conftest beside the name, or patch the name where the reader reads "
        f"it: {unexplained}"
    )


def test_every_exception_row_names_a_live_patch_and_a_live_moved_reader(
    live_scan: Scan,
) -> None:
    found = moved_readers(_REPO, live_scan.sites)
    live = {(s.path, s.function, s.conftest, s.name): readers for s, readers in found.items()}
    stale = {
        key: sorted(set(rows) - live.get(key, set()))
        for key, rows in _MOVED_READERS_THE_PATCH_SKIPS.items()
        if set(rows) - live.get(key, set())
    }
    assert not stale, f"exception rows that no longer match a patch and a moved reader: {stale}"


def test_every_patch_call_in_a_file_that_reaches_a_conftest_is_resolved(live_scan: Scan) -> None:
    assert not live_scan.unresolved, (
        "In a file that reaches a conftest module, these patch calls target a parameter or "
        "local the scan cannot resolve, so it cannot tell whether they patch a conftest. "
        f"Pass the conftest handle directly, or name it at module level: {live_scan.unresolved}"
    )


def test_every_loader_that_names_a_conftest_resolves_to_a_path() -> None:
    assert unresolved_loaders(_REPO) == []


def test_every_name_patched_on_a_conftest_is_resolved(live_scan: Scan) -> None:
    unresolved = [site for site in live_scan.sites if site.name.startswith("<unresolved")]
    assert not unresolved, f"patches whose name the scan cannot work out: {unresolved}"


def test_every_string_plugin_that_patches_a_conftest_has_a_row() -> None:
    found = string_plugins(_REPO)
    listed = {(path, holder) for path, holder, _conftest, _name in _STRING_PLUGIN_PATCHES}
    assert set(found) == listed, (
        "A plugin written as a string imports a conftest and patches it; the scan does not "
        "parse it, so list what it patches in _STRING_PLUGIN_PATCHES."
    )
    for path, holder, _conftest, name in _STRING_PLUGIN_PATCHES:
        assert f'"{name}"' in found[(path, holder)] or f"'{name}'" in found[(path, holder)]


def test_each_string_plugin_row_lists_exactly_the_moved_readers_of_its_name() -> None:
    for (path, holder, conftest, name), rows in _STRING_PLUGIN_PATCHES.items():
        site = PatchSite(path, 0, holder, conftest, name)
        assert moved_readers(_REPO, [site]).get(site, set()) == set(rows), (path, holder, name)


def test_the_scan_finds_the_patch_sites_the_suite_is_known_to_make(live_scan: Scan) -> None:
    seen = {(s.path, s.function, s.conftest, s.name) for s in live_scan.sites}
    paths = {(path, conftest, name) for path, _function, conftest, name in seen}
    assert ("test/test_host_isolation_floor.py", "conftest.py", "_EXECUTOR_JOIN_SECS") in paths
    assert (
        "test/test_conftest_gateway_guard.py",
        "test/conftest.py",
        "_EXPORTED_GATEWAY_PORTS",
    ) in paths
    # Reached only through the helper's parameter, which every caller passes a root copy.
    assert (
        "test/test_xdist_escaped_failure_guard.py",
        "_drive_protocol",
        "conftest.py",
        "time",
    ) in seen
    assert {s.conftest for s in live_scan.sites} == set(_CONFTESTS)
    assert len({s.path for s in live_scan.sites}) >= 5, sorted({s.path for s in live_scan.sites})


def test_each_part_belongs_to_the_conftest_its_name_says() -> None:
    """The rootdir parts are ``conftest_floor_*``, the suite's the other ``conftest_*``.

    The source pins that read a conftest joined with its parts select the parts by
    that naming, so a part bound by the other conftest would drop out of their reach.
    """
    root_parts, suite_parts = (set(conftest_family(_REPO, c)[1:]) for c in _CONFTESTS)
    on_disk = {_rel(_REPO, path) for path in _TEST_DIR.glob("conftest_*.py")}
    floor = {rel for rel in on_disk if rel.startswith("test/conftest_floor_")}
    assert root_parts and suite_parts
    assert root_parts == floor
    assert suite_parts == on_disk - floor


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the test trees, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(_REPO).as_posix() in leaf_test_scope.corpus_gates(_REPO)


# -- the detector on a synthetic tree ---------------------------------------------------

_ROOT_CONFTEST = """\
from conftest_floor_part import MOVED_READ, KEPT_READ, GEN_READ  # noqa: F401


def kept_reader():
    return KEPT_READ
"""
_ROOT_PART = """\
MOVED_READ = 1
KEPT_READ = 2
GEN_READ = 3


def moved_reader():
    return [MOVED_READ for _ in range(1)]


def gen_reader():
    return list(GEN_READ for _ in range(1))
"""
_SUITE_CONFTEST = """\
from conftest_suite_part import SUITE_READ, SUITE_KEPT  # noqa: F401


def kept_suite_reader():
    return SUITE_KEPT
"""
_SUITE_PART = """\
SUITE_READ = 3
SUITE_KEPT = 4


def moved_suite_reader():
    return SUITE_READ
"""
_LOADER_HELPER = """\
import importlib.util
import pathlib

_ROOT_CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "conftest.py"


def _load_root_conftest():
    spec = importlib.util.spec_from_file_location("_private_root", _ROOT_CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()
"""
_IN_APP = "src/kiro_crew/apps/builtins/app/tests"
#: shape -> (directory, conftest the shape reaches, test text with a ``{name}`` slot).
_SHAPES: dict[str, tuple[str, str, str]] = {
    "helper_handle": (
        "test",
        "conftest.py",
        "from loader_helpers import _root\n\n\ndef test_x(monkeypatch):\n"
        "    monkeypatch.setattr(_root, {name!r}, 0)\n",
    ),
    "local_by_path": (
        "test",
        "conftest.py",
        "import importlib.util\nimport pathlib\n\n\ndef test_x(monkeypatch):\n"
        "    spec = importlib.util.spec_from_file_location('c', pathlib.Path(__file__).parents[1] / 'conftest.py')\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    monkeypatch.setattr(module, {name!r}, 0)\n",
    ),
    "get_plugin_fixture": (
        "test",
        "conftest.py",
        "import pathlib\nimport pytest\n\n_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]\n\n\n"
        "@pytest.fixture\ndef root_conftest(request):\n"
        "    return request.config.pluginmanager.get_plugin(str(_REPO_ROOT / 'conftest.py'))\n\n\n"
        "def test_x(root_conftest, monkeypatch):\n    monkeypatch.setattr(root_conftest, {name!r}, 0)\n",
    ),
    "patch_object_in_loop": (
        "test",
        "conftest.py",
        "from unittest import mock\nfrom loader_helpers import _root\n\n\ndef test_x():\n"
        "    for name in ('unrelated', {name!r}):\n        with mock.patch.object(_root, name, 0):\n            pass\n",
    ),
    "attribute_store": (
        "test",
        "conftest.py",
        "from loader_helpers import _root\n\n\ndef test_x():\n    _root.{name} = 0\n",
    ),
    "helper_parameter": (
        "test",
        "conftest.py",
        "from loader_helpers import _load_root_conftest\n\n\ndef _drive(module, monkeypatch):\n"
        "    monkeypatch.setattr(module, {name!r}, 0)\n\n\ndef test_x(monkeypatch):\n"
        "    root = _load_root_conftest()\n    _drive(root, monkeypatch)\n\n\n"
        "def test_y(monkeypatch):\n    _drive(_load_root_conftest(), monkeypatch=monkeypatch)\n",
    ),
    "tuple_from_a_method": (
        "test",
        "conftest.py",
        "from loader_helpers import _root\n\n\nclass TestX:\n    def _probe(self):\n"
        "        return _root, 1\n\n    def test_x(self, monkeypatch):\n"
        "        root, _value = self._probe()\n        monkeypatch.setattr(root, {name!r}, 0)\n",
    ),
    "import_conftest_in_an_app_suite": (
        _IN_APP,
        "conftest.py",
        "import conftest as root\n\n\ndef test_x(monkeypatch):\n"
        "    monkeypatch.setattr(root, {name!r}, 0)\n",
    ),
    "import_conftest_in_test": (
        "test",
        "test/conftest.py",
        "def test_x(monkeypatch):\n    import conftest\n\n"
        "    monkeypatch.setattr(conftest, {name!r}, 0)\n",
    ),
    "os_path_get_plugin": (
        "test",
        "test/conftest.py",
        "import os\nfrom pathlib import Path\n\nTEST_DIR = Path(os.path.abspath(os.path.dirname(__file__)))\n"
        "CONFTEST = TEST_DIR / 'conftest.py'\n\n\ndef test_x(request, monkeypatch):\n"
        "    plugin = request.config.pluginmanager.get_plugin(str(CONFTEST))\n"
        "    monkeypatch.setattr(plugin, {name!r}, 0, raising=False)\n",
    ),
    "suite_plugin_search": (
        "test",
        "test/conftest.py",
        "import pathlib\n\n\ndef _suite(request):\n"
        "    path = pathlib.Path(__file__).with_name('conftest.py').resolve()\n"
        "    for plugin in request.config.pluginmanager.get_plugins():\n"
        "        if pathlib.Path(plugin.__file__).resolve() == path:\n            return plugin\n\n\n"
        "def test_x(request, monkeypatch):\n    monkeypatch.setattr(_suite(request), {name!r}, 0)\n",
    ),
}
#: conftest -> (a name read in a part, that part reader, a name read only in the conftest).
_NAMES = {
    "conftest.py": ("MOVED_READ", "test/conftest_floor_part.py::moved_reader", "KEPT_READ"),
    "test/conftest.py": (
        "SUITE_READ",
        "test/conftest_suite_part.py::moved_suite_reader",
        "SUITE_KEPT",
    ),
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "test").mkdir()
    (tmp_path / "conftest.py").write_text(_ROOT_CONFTEST, encoding="utf-8")
    (tmp_path / "test" / "conftest_floor_part.py").write_text(_ROOT_PART, encoding="utf-8")
    (tmp_path / "test" / "conftest.py").write_text(_SUITE_CONFTEST, encoding="utf-8")
    (tmp_path / "test" / "conftest_suite_part.py").write_text(_SUITE_PART, encoding="utf-8")
    (tmp_path / "test" / "loader_helpers.py").write_text(_LOADER_HELPER, encoding="utf-8")
    return tmp_path


def _write(tree: Path, rel: str, text: str) -> str:
    (tree / rel).parent.mkdir(parents=True, exist_ok=True)
    (tree / rel).write_text(text, encoding="utf-8")
    return rel


def _flagged(tree: Path, shape: str, name: str) -> dict[PatchSite, set[str]]:
    folder, _conftest, text = _SHAPES[shape]
    _write(tree, f"{folder}/test_{shape}.py", text.format(name=name))
    found = scan(tree)
    assert not found.unresolved, found.unresolved
    sites = [site for site in found.sites if site.name == name]
    assert sites, f"the {shape} patch of {name} was not recognised as a conftest patch"
    return moved_readers(tree, sites)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_patch_of_a_reexported_name_with_a_moved_reader_is_flagged(
    tree: Path, shape: str
) -> None:
    conftest = _SHAPES[shape][1]
    moved, reader, _kept = _NAMES[conftest]
    found = _flagged(tree, shape, moved)
    assert {site.conftest for site in found} == {conftest}
    assert list(found.values()) == [{reader}] * len(found)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_patch_whose_readers_all_stay_in_the_conftest_passes(tree: Path, shape: str) -> None:
    assert _flagged(tree, shape, _NAMES[_SHAPES[shape][1]][2]) == {}


def test_a_reader_inside_a_generator_expression_in_a_part_is_flagged(tree: Path) -> None:
    found = _flagged(tree, "helper_handle", "GEN_READ")
    assert list(found.values()) == [{"test/conftest_floor_part.py::gen_reader"}]


def test_a_patch_on_any_other_module_is_not_a_conftest_patch(tree: Path) -> None:
    _write(
        tree,
        "test/test_other.py",
        "import os\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(os, 'MOVED_READ', 0, raising=False)\n",
    )
    found = scan(tree)
    assert [site for site in found.sites if site.path == "test/test_other.py"] == []
    assert found.unresolved == []


_UNCLEAR = {
    "a parameter no caller names": "def _drive(module, monkeypatch):\n"
    "    monkeypatch.setattr(module, 'MOVED_READ', 0)\n",
    "a parameter callers fill differently": "import os\n\n\ndef _drive(module, monkeypatch):\n"
    "    monkeypatch.setattr(module, 'MOVED_READ', 0)\n\n\ndef test_x(monkeypatch):\n"
    "    _drive(_root, monkeypatch)\n    _drive(os, monkeypatch)\n",
    "a loop variable": "def test_x(monkeypatch):\n    for module in (_root,):\n"
    "        monkeypatch.setattr(module, 'MOVED_READ', 0)\n",
    "a method parameter": "class TestX:\n    def _drive(self, module, monkeypatch):\n"
    "        monkeypatch.setattr(module, 'MOVED_READ', 0)\n",
    "a tuple from a call the scan cannot follow": "def test_x(monkeypatch, make):\n"
    "    module, _value = make()\n    monkeypatch.setattr(module, 'MOVED_READ', 0)\n",
}


@pytest.mark.parametrize("shape", sorted(_UNCLEAR))
def test_a_patch_through_a_binding_the_scan_cannot_follow_fails_closed(
    tree: Path, shape: str
) -> None:
    rel = _write(
        tree, "test/test_unclear.py", "from loader_helpers import _root\n\n\n" + _UNCLEAR[shape]
    )
    unresolved = scan(tree).unresolved
    assert [entry.split(":")[0] for entry in unresolved] == [rel], unresolved


@pytest.mark.parametrize("shape", sorted(_UNCLEAR))
def test_the_same_binding_in_a_file_that_reaches_no_conftest_is_left_alone(
    tree: Path, shape: str
) -> None:
    _write(tree, "test/test_unclear.py", "import os\n\n_root = os\n\n\n" + _UNCLEAR[shape])
    assert scan(tree).unresolved == []


def test_a_patch_whose_name_is_not_a_constant_is_reported(tree: Path) -> None:
    _write(
        tree,
        "test/test_name.py",
        "from loader_helpers import _root\n\n\ndef test_x(monkeypatch, request):\n"
        "    monkeypatch.setattr(_root, request.param, 0)\n",
    )
    names = [site.name for site in scan(tree).sites if site.path == "test/test_name.py"]
    assert names == ["<unresolved request.param>"]


def test_a_loader_whose_path_the_scan_cannot_follow_is_reported(tree: Path) -> None:
    rel = _write(
        tree,
        "test/test_loader.py",
        "import importlib.util\n\n\ndef test_x(where):\n"
        "    importlib.util.spec_from_file_location('c', where() / 'conftest.py')\n",
    )
    assert unresolved_loaders(tree) == [f"{rel}:5"]


def test_a_string_plugin_that_patches_a_conftest_is_found_for_listing(tree: Path) -> None:
    rel = _write(
        tree,
        "test/test_plugin.py",
        "import textwrap\n\n_PLUGIN = textwrap.dedent('''\n    import conftest\n\n"
        "    def pytest_configure(config):\n        setattr(conftest, 'MOVED_READ', 0)\n''')\n"
        "_READER = 'import conftest\\nprint(conftest.MOVED_READ)\\n'\n",
    )
    assert set(string_plugins(tree)) == {(rel, "_PLUGIN")}
