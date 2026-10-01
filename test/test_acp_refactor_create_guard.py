"""No test patches a name an ACP facade forwards with ``create=True``.

``kiro_crew.acp.client`` and ``kiro_crew.acp.runtime`` forward a write or delete of a
moved name to the owner module that defines it. ``mock.patch`` restores a name the
facade does not hold by deleting it and then, finding it gone, writing its original
back -- unless ``create`` is true, in which case it skips that write and the owner loses
the name for every later caller in the worker. So such a patch is refused here, by a
scan of the test trees, rather than emulated in the facades.

The scan reads every test file that mentions ``patch`` and a facade's dotted name, and
resolves each call from its syntax tree: which callable is ``mock.patch`` under any
import alias, and which module and name a target means, through imports, name
assignments, ``importlib.import_module`` and ``pytest.importorskip`` of a known string,
module-name constants, f-strings and concatenation. A name is first looked up among the
enclosing functions' parameters, which are unbound here. A target that is a parameter, a
call's result, a name bound only from another call or subscript, or text it cannot spell
is reported as ``<dynamic>`` rather than dropped, because a guard that skips what it
cannot read passes exactly the patch it exists to refuse. A def, a class or a literal is
read as not a facade.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import re
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp import runtime as acp_runtime

# The scan parses every test file once per module; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_acp_refactor_create_guard")

#: Each forwarding facade, by the dotted name a patch target spells.
_FACADES = {module.__name__: module for module in (acp_client, acp_runtime)}
_DYNAMIC = "<dynamic>"
_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "kiro_crew"

#: What a ``mock.patch`` callable resolves to, per module that provides one: the
#: standard library, its PyPI backport, and pytest-mock's ``mocker`` fixture.
_PATCH_FORMS = {
    f"{provider}.{spelling}": form
    for provider in ("unittest.mock", "mock", "mocker")
    for spelling, form in (
        ("patch", "patch"),
        ("patch.object", "object"),
        ("patch.multiple", "multiple"),
    )
}

#: Where each form takes ``create`` when it is passed by position.
_CREATE_POSITION = {"patch": 3, "object": 4, "multiple": 2}

#: ``patch.multiple`` parameters that are not names to patch.
_MULTIPLE_PARAMETERS = frozenset(
    {"target", "spec", "create", "spec_set", "autospec", "new_callable"}
)

_THIS_FILE = Path(__file__).resolve().relative_to(_REPO).as_posix()
_PREMISE = "test_the_premise_create_true_through_the_facade_unbinds_the_owner"

#: Deliberate ``create=True`` premises, as ``(path relative to the repository, test
#: function)``. The raw scan must equal this set exactly, one hit each.
_ALLOWED: frozenset[tuple[str, str]] = frozenset({(_THIS_FILE, _PREMISE)})

_LOCAL = "<local>"
_UNKNOWN = "<unknown>"

#: Expressions whose value is plainly not a module the file imported.
_LITERALS = (
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Lambda,
    ast.JoinedStr,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

#: Calls whose result is the module their first argument names.
_IMPORTERS = frozenset({"importlib.import_module", "pytest.importorskip"})


def _rank(bound: str | None) -> int:
    """Which of two bindings of one name the scan keeps: the one closer to a facade."""
    if bound is None:
        return 0
    if bound == _LOCAL:
        return 1
    if bound == _UNKNOWN:
        return 2
    return 4 if bound in _FACADES else 3


def _text_rank(text: str | None) -> int:
    """Which of two string bindings of one name the scan keeps: one spelling a facade
    path wins over any other, and otherwise the first binding stands."""
    if text is None:
        return 0
    return 2 if any(text == f or text.startswith(f + ".") for f in _FACADES) else 1


class _Scope:
    """What each name in one file means, resolved to a fixed point.

    A name maps to the dotted path of the module or object it is bound to,
    ``"<local>"`` for a def, class or literal, ``"<unknown>"`` for a value only a
    call or subscript produced, or is absent when the file never binds it (a
    parameter, such as a fixture). String constants are kept apart, for resolving
    patch targets spelled as text. When one name has several bindings, the one
    closest to a facade wins.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.names: dict[str, str] = {"mocker": "mocker"}
        self.strings: dict[str, str] = {}
        self._views: dict[frozenset[str], _Scope] = {}
        assignments: list[tuple[str, ast.expr]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.names[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".", 1)[0]
                        self.names[root] = root
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.names.setdefault(node.name, _LOCAL)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments.append((target.id, node.value))
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    assignments.append((node.target.id, node.value))
        for _ in range(len(assignments) + 1):
            changed = False
            for name, value in assignments:
                text = self.text(value)
                if text is not None and _text_rank(text) > _text_rank(self.strings.get(name)):
                    self.strings[name] = text
                    changed = True
                bound = self.module(value)
                bound = _UNKNOWN if bound is None else bound
                if _rank(bound) > _rank(self.names.get(name)):
                    self.names[name] = bound
                    changed = True
            if not changed:
                break

    def without(self, parameters: frozenset[str]) -> _Scope:
        """This scope as seen inside a function whose *parameters* shadow it.

        A parameter is unbound here, whatever the file binds under that name; only
        ``mocker`` stays pytest-mock's fixture.
        """
        shadowed = parameters - {"mocker"}
        if not shadowed & (set(self.names) | set(self.strings)):
            return self
        view = self._views.get(shadowed)
        if view is None:
            view = copy.copy(self)
            view.names = {k: v for k, v in self.names.items() if k not in shadowed}
            view.strings = {k: v for k, v in self.strings.items() if k not in shadowed}
            self._views[shadowed] = view
        return view

    def module(self, node: ast.expr) -> str | None:
        """The dotted path *node* names, ``"<local>"`` for a def, class or literal,
        or None when the file does not say."""
        if isinstance(node, ast.Name):
            bound = self.names.get(node.id)
            return None if bound == _UNKNOWN else bound
        if isinstance(node, ast.Attribute):
            base = self.module(node.value)
            if base is None or base == _LOCAL:
                return base
            return f"{base}.{node.attr}"
        if isinstance(node, ast.Call):
            if self.module(node.func) in _IMPORTERS and node.args:
                name = self.text(node.args[0])
                package = self.text(node.args[1]) if len(node.args) > 1 else None
                if name is not None and name.startswith(".") and package is not None:
                    return importlib.util.resolve_name(name, package)
                return name
            return None
        if isinstance(node, ast.Subscript) and self.module(node.value) == "sys.modules":
            return self.text(node.slice)
        if isinstance(node, _LITERALS):
            return _LOCAL
        return None

    def text(self, node: ast.expr) -> str | None:
        """The string *node* spells, or None when it cannot be known from the file."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self.strings.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "__name__":
            base = self.module(node.value)
            return base if base not in (None, _LOCAL) else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.text(node.left), self.text(node.right)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    if part.conversion != -1 or part.format_spec is not None:
                        return None
                    part_text = self.text(part.value)
                else:
                    part_text = self.text(part)
                if part_text is None:
                    return None
                parts.append(part_text)
            return "".join(parts)
        return None


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((k.value for k in call.keywords if k.arg == keyword), None)


def _may_create(call: ast.Call, form: str) -> bool:
    """False only when ``create`` is absent or the literal ``False``. A ``*args``
    positional at or before ``create``'s slot may carry it, so it counts as present."""
    position = _CREATE_POSITION[form]
    if any(isinstance(arg, ast.Starred) for arg in call.args[: position + 1]):
        return True
    create = _argument(call, position, "create")
    if create is None:
        return any(k.arg is None for k in call.keywords)
    return not (isinstance(create, ast.Constant) and create.value is False)


def _patched_names(call: ast.Call, form: str, scope: _Scope) -> list[tuple[str, str]]:
    """``(facade, name)`` for each facade name *call* patches; ``<dynamic>`` stands in
    for a facade or name it cannot resolve."""
    if form == "patch":
        target = _argument(call, 0, "target")
        text = scope.text(target) if target is not None else None
        if text is None:
            return [(_DYNAMIC, _DYNAMIC)]
        owner, _, name = text.rpartition(".")
        return [(owner, name)] if owner in _FACADES else []
    target = _argument(call, 0, "target")
    if target is None:
        return [(_DYNAMIC, _DYNAMIC)]
    module = scope.module(target) if not isinstance(target, ast.Constant) else None
    if module is None and isinstance(target, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        module = scope.text(target)
    if module is None:
        return [(_DYNAMIC, _DYNAMIC)]
    if module not in _FACADES:
        return []
    if form == "object":
        attribute = _argument(call, 1, "attribute")
        name = scope.text(attribute) if attribute is not None else None
        return [(module, name if name is not None else _DYNAMIC)]
    names = [(module, k.arg) for k in call.keywords if k.arg and k.arg not in _MULTIPLE_PARAMETERS]
    if any(k.arg is None for k in call.keywords):
        names.append((module, _DYNAMIC))
    return names


def _forwarded(facade: str, name: str) -> bool:
    return facade == _DYNAMIC or name == _DYNAMIC or name in _FACADES[facade]._EXPORTS


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
    args = node.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return frozenset(arg.arg for arg in named if arg is not None)


def _create_patches(tree: ast.Module) -> list[tuple[int, str, str]]:
    """``(line, test function, name)`` for each patch that may create a forwarded name."""
    scope = _Scope(tree)
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, function: str, parameters: frozenset[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Decorators and defaults run in the enclosing scope; the body sees the
            # function's own parameters over whatever the file binds.
            outer = [*node.decorator_list, *node.args.defaults]
            outer += [d for d in node.args.kw_defaults if d is not None]
            for child in outer:
                visit(child, node.name, parameters)
            inner = parameters | _parameters(node)
            for child in node.body:
                visit(child, node.name, inner)
            return
        if isinstance(node, ast.Lambda):
            visit(node.body, function, parameters | _parameters(node))
            return
        if isinstance(node, ast.Call):
            view = scope.without(parameters)
            form = _PATCH_FORMS.get(view.module(node.func) or "")
            if form is not None and _may_create(node, form):
                for facade, name in _patched_names(node, form, view):
                    if _forwarded(facade, name):
                        found.append((node.lineno, function, name))
        for child in ast.iter_child_nodes(node):
            visit(child, function, parameters)

    visit(tree, "<module>", frozenset())
    return found


#: ``from kiro_crew import acp`` (or ``acp as X``), also inside a parenthesized list.
_FROM_IMPORT_ACP = re.compile(r"from\s+kiro_crew\s+import\s+\(?[\w\s,]*?\bacp\b(?!_)")


def _worth_parsing(text: str) -> bool:
    """Whether a test file could hold a patch of a facade at all: it mentions a patch
    and a facade's module path, or imports the ``kiro_crew.acp`` package itself."""
    if "patch" not in text:
        return False
    return "kiro_crew.acp" in text or _FROM_IMPORT_ACP.search(text) is not None


_IMPORT_CLIENT = "from kiro_crew.acp import client\n"
_IMPORT_RUNTIME = "from kiro_crew.acp import runtime\n"
_FROM_MOCK = "from unittest import mock\n"
_CLIENT = acp_client.__name__
_RUNTIME = acp_runtime.__name__

#: A forwarded name of each facade, and a name each binds itself (restored by
#: assignment).
_MOVED = "_kill_escaped_children"
_MOVED_RT = "_resolve_session_start_concurrency"
_KEPT = "_mise_which"
_KEPT_RT = "_INITIALIZE_TIMEOUT"


#: ``(source, expected hits)``: every rule answered both ways, a must-flag case and a
#: must-ignore case each.
_CASES: list[tuple[str, list[str]]] = [
    # (1) the patch callable under any import alias, and as a decorator
    (
        f'from unittest.mock import patch as _p\n_p("{_CLIENT}.{_MOVED}", create=True)',
        [_MOVED],
    ),
    (f'from elsewhere import patch as _p\n_p("{_CLIENT}.{_MOVED}", create=True)', []),
    (
        _IMPORT_CLIENT
        + f'import unittest.mock as um\num.patch.object(client, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT
        + f"from unittest import mock as m\nm.patch.multiple(client, {_MOVED}=1, create=True)",
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT + "from unittest.mock import patch\n"
        f'@patch.object(client, "{_MOVED}", create=True)\ndef test_x(): pass',
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT
        + f'def test_x(mocker): mocker.patch.object(client, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT + f'def test_x(other): other.patch.object(client, "{_MOVED}", create=True)',
        [],
    ),
    # the runtime facade is scanned the same way, and each facade's table is its own
    (
        _IMPORT_RUNTIME + _FROM_MOCK + f'mock.patch.object(runtime, "{_MOVED_RT}", create=True)',
        [_MOVED_RT],
    ),
    (_FROM_MOCK + f'mock.patch("{_RUNTIME}.{_MOVED_RT}", create=True)', [_MOVED_RT]),
    (_FROM_MOCK + f'mock.patch("{_RUNTIME}.{_MOVED}", create=True)', []),
    (_FROM_MOCK + f'mock.patch("{_RUNTIME}.{_KEPT_RT}", create=True)', []),
    # (2) targets spelled as f-strings, module-name constants and concatenation
    (
        _IMPORT_CLIENT + _FROM_MOCK + f'mock.patch(f"{{client.__name__}}.{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + f'_MOD = "{_CLIENT}"\nmock.patch(f"{{_MOD}}.{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + f'_MOD = "{_CLIENT}"\nmock.patch(_MOD + ".{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + f'_MOD = "kiro_crew.other"\nmock.patch(f"{{_MOD}}.{_MOVED}", create=True)',
        [],
    ),
    (_FROM_MOCK + f'import os\nmock.patch(f"{{os.__name__}}.{_MOVED}", create=True)', []),
    # (3) the target= and attribute= keyword forms
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + f'mock.patch.object(target=client, attribute="{_MOVED}", create=True)',
        [_MOVED],
    ),
    (_FROM_MOCK + f'mock.patch(target="{_CLIENT}.{_MOVED}", create=True)', [_MOVED]),
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + 'mock.patch.object(target=client, attribute="api_new_thing", create=True)',
        [],
    ),
    # (4) any create that is not the literal False, by keyword or position
    (_IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_MOVED}", create=1)', [_MOVED]),
    (
        _IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_MOVED}", None, None, flag)',
        [_MOVED],
    ),
    (_IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_MOVED}", create=False)', []),
    (_IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_MOVED}")', []),
    # a name the facade binds itself is restored by assignment, create or not
    (_IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_KEPT}", create=True)', []),
    # (5) what cannot be resolved is reported, never dropped
    (_IMPORT_CLIENT + _FROM_MOCK + "mock.patch.object(client, name, create=True)", [_DYNAMIC]),
    (
        _IMPORT_CLIENT + _FROM_MOCK + 'def test_x(module): mock.patch(f"{module}.x", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_CLIENT + _FROM_MOCK + "mock.patch.multiple(client, create=True, **names)",
        [_DYNAMIC],
    ),
    (
        _IMPORT_CLIENT + _FROM_MOCK + 'def test_x(obj): mock.patch.object(obj, "x", create=True)',
        [_DYNAMIC],
    ),
    (_FROM_MOCK + 'import json\nmock.patch.object(json, "x", create=True)', []),
    # (6) aliases bound by assignment, resolved to a fixed point
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + f'a = client\nb = a\nmock.patch.object(b, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + f'import importlib\nh = importlib.import_module("{_CLIENT}")\n'
        f'mock.patch.object(h, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + 'import importlib\nh = importlib.import_module("json")\n'
        f'mock.patch.object(h, "{_MOVED}", create=True)',
        [],
    ),
    # the owner itself is not the facade: its own binding is restored by assignment
    (
        "from kiro_crew.acp import runtime_process_tree\n"
        + _FROM_MOCK
        + f'mock.patch.object(runtime_process_tree, "{_MOVED}", create=True)',
        [],
    ),
    # a parameter shadows what the file binds under that name, even a fixture def
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + "def facade():\n    return client\n"
        + f'def test_x(facade): mock.patch.object(facade, "{_MOVED}", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + f'def test_x(client): mock.patch.object(client, "{_MOVED}", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + "def facade():\n    return client\n"
        + f'def test_x(facade): mock.patch.object(facade, "{_MOVED}")',
        [],
    ),
    # a decorator runs outside the function it decorates, so its parameters do not shadow it
    (
        _IMPORT_CLIENT + "from unittest.mock import patch\n"
        f'@patch.object(client, "{_MOVED}", create=True)\ndef test_x(client): pass',
        [_MOVED],
    ),
    # a call's result, or a name bound only by a call or subscript, is not known here
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + "def _facade():\n    return client\n"
        + f'mock.patch.object(_facade(), "{_MOVED}", create=True)',
        [_DYNAMIC],
    ),
    (
        "import kiro_crew.acp\n"
        + _FROM_MOCK
        + f'h = getattr(kiro_crew.acp, "client")\nmock.patch.object(h, "{_MOVED}", create=True)',
        [_DYNAMIC],
    ),
    (
        "import sys\n"
        + _FROM_MOCK
        + f'h = sys.modules["{_CLIENT}"]\nmock.patch.object(h, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + f'h = [client]\nmock.patch.object(h, "{_MOVED}", create=True)',
        [],
    ),
    # pytest.importorskip of a known string, like import_module
    (
        _FROM_MOCK + f'import pytest\nh = pytest.importorskip("{_CLIENT}")\n'
        f'mock.patch.object(h, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT + _FROM_MOCK + 'import pytest\nh = pytest.importorskip("json")\n'
        f'mock.patch.object(h, "{_MOVED}", create=True)',
        [],
    ),
    # a ``*args`` positional may carry create; a ``**kwargs`` one is (5) above
    (
        _IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_MOVED}", *extra)',
        [_MOVED],
    ),
    (
        _IMPORT_CLIENT
        + _FROM_MOCK
        + f'mock.patch.object(client, "{_MOVED}", None, None, False, *x)',
        [],
    ),
    # a rebound string constant: the binding that spells a facade path wins
    (
        _FROM_MOCK
        + f'T = "kiro_crew.other.x"\nT = "{_CLIENT}.{_MOVED}"\nmock.patch(T, create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + 'T = "kiro_crew.other.x"\nT = "kiro_crew.other.y"\nmock.patch(T, create=True)',
        [],
    ),
    # import_module of a relative name with its package
    (
        _FROM_MOCK + 'import importlib\nh = importlib.import_module(".client", "kiro_crew.acp")\n'
        f'mock.patch.object(h, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        _FROM_MOCK + 'import importlib\nh = importlib.import_module(".other", "kiro_crew.acp")\n'
        f'mock.patch.object(h, "{_MOVED}", create=True)',
        [],
    ),
    # the facade as an attribute of the package imported by name
    (
        "from kiro_crew import acp\n"
        + _FROM_MOCK
        + f'mock.patch.object(acp.client, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    # positional create with no ``create`` token, and the package imported whole
    (
        _IMPORT_CLIENT + _FROM_MOCK + f'mock.patch.object(client, "{_MOVED}", None, None, True)',
        [_MOVED],
    ),
    (
        "import kiro_crew.acp\n"
        + _FROM_MOCK
        + f'mock.patch.object(kiro_crew.acp.client, "{_MOVED}", create=True)',
        [_MOVED],
    ),
    (
        "import kiro_crew.acp.runtime\n"
        + _FROM_MOCK
        + f'mock.patch.object(kiro_crew.acp.runtime, "{_MOVED_RT}", create=True)',
        [_MOVED_RT],
    ),
]


@pytest.mark.parametrize(("source", "expected"), _CASES)
def test_the_detector_answers_both_ways(source: str, expected: list[str]) -> None:
    assert [name for _line, _function, name in _create_patches(ast.parse(source))] == expected


def test_the_case_names_are_what_the_cases_claim() -> None:
    """Each forwarded name is forwarded and each kept one is not, or the cases prove
    nothing."""
    assert _MOVED in acp_client._EXPORTS and _MOVED not in acp_runtime._EXPORTS
    assert _MOVED_RT in acp_runtime._EXPORTS
    assert _KEPT not in acp_client._EXPORTS and _KEPT in vars(acp_client)
    assert _KEPT_RT not in acp_runtime._EXPORTS and _KEPT_RT in vars(acp_runtime)


def test_no_src_module_binds_a_facade_at_module_level() -> None:
    """The scan knows each facade by its dotted name. A src module that bound one at
    import time would be another spelling of it, which this guard would have to learn;
    today every src reader imports a facade inside a function."""
    bound: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "kiro_crew.acp" not in text:
            continue
        for node in ast.parse(text).body:
            if isinstance(node, ast.Import):
                bound += [
                    f"{path.name}:{node.lineno}"
                    for alias in node.names
                    if alias.name in _FACADES and alias.asname
                ]
            elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.acp":
                bound += [
                    f"{path.name}:{node.lineno}"
                    for alias in node.names
                    if f"kiro_crew.acp.{alias.name}" in _FACADES
                ]
    assert bound == [], bound


def test_the_premise_create_true_through_the_facade_unbinds_the_owner() -> None:
    """Why the scan exists: the one ``create=True`` patch it allows, run for real."""
    from kiro_crew.acp import runtime_process_tree

    original = vars(runtime_process_tree)["_is_our_child"]
    try:
        with mock.patch.object(acp_client, "_is_our_child", create=True):
            pass
        assert "_is_our_child" not in vars(runtime_process_tree)
    finally:
        runtime_process_tree._is_our_child = original


def test_every_must_flag_case_passes_the_prefilter() -> None:
    """A file the prefilter skips is never parsed, so each form the detector flags must
    also survive the prefilter; and a file that names no facade is skipped."""
    flagged = [source for source, expected in _CASES if expected]
    assert flagged and [source for source in flagged if not _worth_parsing(source)] == []
    assert not _worth_parsing(
        "from unittest import mock\nfrom kiro_crew import agent_state\n"
        "mock.patch.object(agent_state, 'x')\n"
    )
    assert _worth_parsing(
        "from unittest import mock\nfrom kiro_crew.acp import (\n    client,\n)\n"
        "mock.patch.object(client, 'x')\n"
    )
    assert not _worth_parsing("from unittest import mock\nmock.patch.object(json, 'x')\n")
    assert not _worth_parsing("from kiro_crew.acp import client\n")
    assert _worth_parsing("from kiro_crew import acp\nmock.patch.object(acp.client, 'x')\n")
    assert not _worth_parsing("from kiro_crew import acp_tool_gate\nmock.patch.object(x, 'y')\n")


def test_no_test_patches_a_forwarded_name_that_it_may_create() -> None:
    roots = [_REPO / "test", *sorted(_SRC.rglob("tests"))]
    scanned = parsed = 0
    hits: dict[tuple[str, str], list[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            scanned += 1
            if not _worth_parsing(text):
                continue
            parsed += 1
            relative = path.relative_to(_REPO).as_posix()
            for line, function, name in _create_patches(ast.parse(text)):
                hits.setdefault((relative, function), []).append(f"line {line}: {name}")
    assert scanned > 100, f"the scan read only {scanned} test files, so it measured nothing"
    assert parsed > 50, f"the scan parsed only {parsed} files, so its prefilter is too tight"
    assert set(hits) == _ALLOWED, hits
    assert [len(hits[key]) for key in sorted(_ALLOWED)] == [1] * len(_ALLOWED), hits
