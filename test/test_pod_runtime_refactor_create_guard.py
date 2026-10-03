"""No test patches a name the pod runtime forwards with ``create=True``.

``kiro_crew.pod.runtime`` forwards a delete of a moved name to the module that owns
it. ``mock.patch`` restores a name the runtime does not hold by deleting it and then,
finding it gone, writing its original back -- unless ``create`` is true, in which case
it skips that write and the owner loses the name for every later caller in the
worker. So such a patch is refused here, by a scan of the test trees, rather than
emulated in the runtime.

The scan reads every test file that mentions ``patch`` and a package holding a
binding of the runtime (``pod``, ``dev_fleet``), and resolves each call from its
syntax tree: which callable is ``mock.patch`` under any import alias, and which module
and name a target means, through imports, name assignments,
``importlib.import_module`` and ``pytest.importorskip`` of a known string,
module-name constants, f-strings and concatenation. The runtime is reached under its
own name and as the ``rt`` a src module binds it to at module level (the pod CLI's and
Dev Fleet's), so ``patch.object(pod_cli.rt, ...)`` is the same target. A name is
first looked up among the enclosing functions' parameters, which are unbound here.
A target that is a parameter, a call's result, a name bound only from another call
or subscript, or text it cannot spell is reported as ``<dynamic>`` rather than
dropped, because a guard that skips what it cannot read passes exactly the patch it
exists to refuse. A def, a class or a literal is read as not the runtime.
"""

from __future__ import annotations

import ast
import copy
import importlib
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest
from stale_package_attribute import package_attribute_replaced

from kiro_crew.pod import runtime as rt

_FACADE = rt.__name__
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
_PREMISE = "test_the_premise_create_true_through_the_runtime_unbinds_the_owner"

#: Deliberate ``create=True`` premises, as ``(path relative to the repository, test
#: function)``. The raw scan must equal this set exactly, one hit each.
_ALLOWED: frozenset[tuple[str, str]] = frozenset({(_THIS_FILE, _PREMISE)})


def _module_level(body: list[ast.stmt]) -> list[ast.stmt]:
    """Statements that run at import, through ``if``/``try``/``with`` but no def."""
    found: list[ast.stmt] = []
    for node in body:
        found.append(node)
        if isinstance(node, (ast.If, ast.For, ast.While, ast.With)):
            found += _module_level(node.body) + _module_level(getattr(node, "orelse", []))
        elif isinstance(node, ast.Try):
            found += _module_level(node.body) + _module_level(node.orelse)
            found += _module_level(node.finalbody)
            for handler in node.handlers:
                found += _module_level(handler.body)
    return found


def _module_name(path: Path) -> str:
    parts = path.relative_to(_SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _facade_paths() -> frozenset[str]:
    """The runtime's dotted name, plus ``<module>.<name>`` for each src module that
    binds it at module level -- another spelling of the same object."""
    paths = {_FACADE}
    package, _, leaf = _FACADE.rpartition(".")
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if package not in text:
            continue
        module = _module_name(path)
        for node in _module_level(ast.parse(text).body):
            if isinstance(node, ast.Import):
                paths.update(
                    f"{module}.{alias.asname}"
                    for alias in node.names
                    if alias.name == _FACADE and alias.asname
                )
            elif isinstance(node, ast.ImportFrom) and node.module == package and not node.level:
                paths.update(
                    f"{module}.{alias.asname or alias.name}"
                    for alias in node.names
                    if alias.name == leaf
                )
    return frozenset(paths)


_FACADE_PATHS = _facade_paths()


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
    """Which of two bindings of one name the scan keeps: the one closer to the runtime."""
    if bound is None:
        return 0
    if bound == _LOCAL:
        return 1
    if bound == _UNKNOWN:
        return 2
    return 4 if bound in _FACADE_PATHS else 3


class _Scope:
    """What each name in one file means, resolved to a fixed point.

    A name maps to the dotted path of the module or object it is bound to,
    ``"<local>"`` for a def, class or literal, ``"<unknown>"`` for a value only a
    call or subscript produced, or is absent when the file never binds it (a
    parameter, such as a fixture). String constants are kept apart, for resolving
    patch targets spelled as text. When one name has several bindings, the one
    closest to the runtime wins.
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
                if text is not None and name not in self.strings:
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
                return self.text(node.args[0])
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
    """False only when ``create`` is absent or the literal ``False``."""
    create = _argument(call, _CREATE_POSITION[form], "create")
    if create is None:
        return any(k.arg is None for k in call.keywords)
    return not (isinstance(create, ast.Constant) and create.value is False)


def _patched_names(call: ast.Call, form: str, scope: _Scope) -> list[str]:
    """The forwarded names *call* patches, ``<dynamic>`` for what it cannot resolve."""
    if form == "patch":
        target = _argument(call, 0, "target")
        text = scope.text(target) if target is not None else None
        if text is None:
            return [_DYNAMIC]
        owner, _, name = text.rpartition(".")
        return [name] if owner in _FACADE_PATHS else []
    target = _argument(call, 0, "target")
    if target is None:
        return [_DYNAMIC]
    module = scope.module(target) if not isinstance(target, ast.Constant) else None
    if module is None and isinstance(target, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        module = scope.text(target)
    if module is None:
        return [_DYNAMIC]
    if module not in _FACADE_PATHS:
        return []
    if form == "object":
        attribute = _argument(call, 1, "attribute")
        name = scope.text(attribute) if attribute is not None else None
        return [name if name is not None else _DYNAMIC]
    names = [k.arg for k in call.keywords if k.arg and k.arg not in _MULTIPLE_PARAMETERS]
    if any(k.arg is None for k in call.keywords):
        names.append(_DYNAMIC)
    return names


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
                for name in _patched_names(node, form, view):
                    if name == _DYNAMIC or name in rt._EXPORTS:
                        found.append((node.lineno, function, name))
        for child in ast.iter_child_nodes(node):
            visit(child, function, parameters)

    visit(tree, "<module>", frozenset())
    return found


def _package_leaves() -> frozenset[str]:
    """The last segment of each package that holds a binding of the runtime: a file
    that reaches the runtime by any importable spelling names one of them."""
    leaves = {_FACADE.rpartition(".")[0].rpartition(".")[2]}
    for path in _FACADE_PATHS - {_FACADE}:
        binding_module = path.rpartition(".")[0]
        leaves.add(binding_module.rpartition(".")[0].rpartition(".")[2])
    return frozenset(leaves)


_LEAVES = _package_leaves()


def _worth_parsing(text: str) -> bool:
    """Whether a test file could hold a patch of the runtime at all."""
    return "patch" in text and any(leaf in text for leaf in _LEAVES)


_IMPORT_RT = "from kiro_crew.pod import runtime as rt\n"
_FROM_MOCK = "from unittest import mock\n"


#: ``(source, expected hits)``: every rule answered both ways, a must-flag case and a
#: must-ignore case each.
_CASES: list[tuple[str, list[str]]] = [
    # (1) the patch callable under any import alias, and as a decorator
    (
        'from unittest.mock import patch as _p\n_p("' + _FACADE + '.stop_pod", create=True)',
        ["stop_pod"],
    ),
    ('from elsewhere import patch as _p\n_p("' + _FACADE + '.stop_pod", create=True)', []),
    (
        _IMPORT_RT + 'import unittest.mock as um\num.patch.object(rt, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _IMPORT_RT
        + "from unittest import mock as m\nm.patch.multiple(rt, stop_pod=1, create=True)",
        ["stop_pod"],
    ),
    (
        _IMPORT_RT + "from unittest.mock import patch\n"
        '@patch.object(rt, "stop_pod", create=True)\ndef test_x(): pass',
        ["stop_pod"],
    ),
    (
        _IMPORT_RT + 'def test_x(mocker): mocker.patch.object(rt, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (_IMPORT_RT + 'def test_x(other): other.patch.object(rt, "stop_pod", create=True)', []),
    # (2) targets spelled as f-strings, module-name constants and concatenation
    (
        _IMPORT_RT + _FROM_MOCK + 'mock.patch(f"{rt.__name__}.stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _FROM_MOCK + f'_MOD = "{_FACADE}"\nmock.patch(f"{{_MOD}}.stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _FROM_MOCK + f'_MOD = "{_FACADE}"\nmock.patch(_MOD + ".stop_pod", create=True)',
        ["stop_pod"],
    ),
    (_FROM_MOCK + '_MOD = "kiro_crew.other"\nmock.patch(f"{_MOD}.stop_pod", create=True)', []),
    (_FROM_MOCK + 'import os\nmock.patch(f"{os.__name__}.stop_pod", create=True)', []),
    # (3) the target= and attribute= keyword forms
    (
        _IMPORT_RT + _FROM_MOCK + 'mock.patch.object(target=rt, attribute="stop_pod", create=True)',
        ["stop_pod"],
    ),
    (_FROM_MOCK + f'mock.patch(target="{_FACADE}.stop_pod", create=True)', ["stop_pod"]),
    (
        _IMPORT_RT
        + _FROM_MOCK
        + 'mock.patch.object(target=rt, attribute="api_new_thing", create=True)',
        [],
    ),
    # (4) any create that is not the literal False, by keyword or position
    (_IMPORT_RT + _FROM_MOCK + 'mock.patch.object(rt, "stop_pod", create=1)', ["stop_pod"]),
    (
        _IMPORT_RT + _FROM_MOCK + 'mock.patch.object(rt, "stop_pod", None, None, flag)',
        ["stop_pod"],
    ),
    (_IMPORT_RT + _FROM_MOCK + 'mock.patch.object(rt, "stop_pod", create=False)', []),
    (_IMPORT_RT + _FROM_MOCK + 'mock.patch.object(rt, "stop_pod")', []),
    # a name the runtime binds itself is restored by assignment, create or not
    (_IMPORT_RT + _FROM_MOCK + 'mock.patch.object(rt, "IS_WINDOWS", create=True)', []),
    # (5) what cannot be resolved is reported, never dropped
    (_IMPORT_RT + _FROM_MOCK + "mock.patch.object(rt, name, create=True)", [_DYNAMIC]),
    (
        _IMPORT_RT + _FROM_MOCK + 'def test_x(module): mock.patch(f"{module}.x", create=True)',
        [_DYNAMIC],
    ),
    (_IMPORT_RT + _FROM_MOCK + "mock.patch.multiple(rt, create=True, **names)", [_DYNAMIC]),
    (
        _IMPORT_RT + _FROM_MOCK + 'def test_x(obj): mock.patch.object(obj, "x", create=True)',
        [_DYNAMIC],
    ),
    (_FROM_MOCK + 'import json\nmock.patch.object(json, "x", create=True)', []),
    # (6) aliases bound by assignment, resolved to a fixed point
    (
        _IMPORT_RT + _FROM_MOCK + 'a = rt\nb = a\nmock.patch.object(b, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _FROM_MOCK + f'import importlib\nh = importlib.import_module("{_FACADE}")\n'
        'mock.patch.object(h, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _FROM_MOCK + 'import importlib\nh = importlib.import_module("json")\n'
        'mock.patch.object(h, "stop_pod", create=True)',
        [],
    ),
    # the runtime reached as the ``rt`` a src module binds it to
    (
        "from kiro_crew.pod import cli as pod_cli\n"
        + _FROM_MOCK
        + 'mock.patch.object(pod_cli.rt, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        "from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod\n"
        + _FROM_MOCK
        + 'mock.patch.object(runtime_mod.rt, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _FROM_MOCK
        + 'mock.patch("kiro_crew.apps.builtins.dev_fleet.runtime.rt.stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        "from kiro_crew.pod import cli as pod_cli\n"
        + _FROM_MOCK
        + 'mock.patch.object(pod_cli, "stop_pod", create=True)',
        [],
    ),
    # a parameter shadows what the file binds under that name, even a fixture def
    (
        _IMPORT_RT
        + _FROM_MOCK
        + "def runtime():\n    return rt\n"
        + 'def test_x(runtime): mock.patch.object(runtime, "stop_pod", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_RT + _FROM_MOCK + 'def test_x(rt): mock.patch.object(rt, "stop_pod", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_RT
        + _FROM_MOCK
        + "def runtime():\n    return rt\n"
        + 'def test_x(runtime): mock.patch.object(runtime, "stop_pod")',
        [],
    ),
    # a decorator runs outside the function it decorates, so its parameters do not shadow it
    (
        _IMPORT_RT + "from unittest.mock import patch\n"
        '@patch.object(rt, "stop_pod", create=True)\ndef test_x(rt): pass',
        ["stop_pod"],
    ),
    # a call's result, or a name bound only by a call or subscript, is not known here
    (
        _IMPORT_RT
        + _FROM_MOCK
        + "def _rt():\n    return rt\n"
        + 'mock.patch.object(_rt(), "stop_pod", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_RT
        + _FROM_MOCK
        + 'h = getattr(pod_cli, "rt")\nmock.patch.object(h, "stop_pod", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_RT
        + _FROM_MOCK
        + 'h = vars(pod_cli)["rt"]\nmock.patch.object(h, "stop_pod", create=True)',
        [_DYNAMIC],
    ),
    (
        _IMPORT_RT + _FROM_MOCK + 'h = [rt]\nmock.patch.object(h, "stop_pod", create=True)',
        [],
    ),
    # pytest.importorskip of a known string, like import_module
    (
        _FROM_MOCK + f'import pytest\nh = pytest.importorskip("{_FACADE}")\n'
        'mock.patch.object(h, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        _IMPORT_RT + _FROM_MOCK + 'import pytest\nh = pytest.importorskip("json")\n'
        'mock.patch.object(h, "stop_pod", create=True)',
        [],
    ),
    # positional create with no ``create`` token, and the package imported whole
    (
        _IMPORT_RT + _FROM_MOCK + 'mock.patch.object(rt, "stop_pod", None, None, True)',
        ["stop_pod"],
    ),
    (
        "from kiro_crew import pod\n"
        + _FROM_MOCK
        + 'mock.patch.object(pod.runtime, "stop_pod", create=True)',
        ["stop_pod"],
    ),
    (
        "import kiro_crew.pod.runtime\n"
        + _FROM_MOCK
        + 'mock.patch.object(kiro_crew.pod.runtime, "stop_pod", create=True)',
        ["stop_pod"],
    ),
]


@pytest.mark.parametrize(("source", "expected"), _CASES)
def test_the_detector_answers_both_ways(source: str, expected: list[str]) -> None:
    assert [name for _line, _function, name in _create_patches(ast.parse(source))] == expected


def test_the_runtime_is_found_under_every_name_src_binds_it_to() -> None:
    assert {
        _FACADE,
        "kiro_crew.pod.cli.rt",
        "kiro_crew.apps.builtins.dev_fleet.runtime.rt",
    } <= _FACADE_PATHS


@pytest.mark.parametrize("split", [False, True], ids=["as-imported", "package-attribute-stale"])
def test_the_premise_create_true_through_the_runtime_unbinds_the_owner(split: bool) -> None:
    """Why the scan exists: the one ``create=True`` patch it allows, run for real.

    The owner is read from ``sys.modules``, where the runtime writes. Its package
    attribute can name another copy once an earlier test in the worker imports it
    fresh, so reading the attribute would make this check depend on test order.
    """
    owner = "kiro_crew.pod.runtime_ports"
    with package_attribute_replaced(owner) if split else nullcontext():
        runtime_ports = importlib.import_module(owner)
        original = vars(runtime_ports)["operator_pinned"]
        try:
            with mock.patch.object(rt, "operator_pinned", create=True):
                pass
            assert "operator_pinned" not in vars(runtime_ports)
        finally:
            vars(runtime_ports)["operator_pinned"] = original


def test_every_must_flag_case_passes_the_prefilter() -> None:
    """A file the prefilter skips is never parsed, so each form the detector flags must
    also survive the prefilter; and a file that names no such package is skipped."""
    flagged = [source for source, expected in _CASES if expected]
    assert flagged and [source for source in flagged if not _worth_parsing(source)] == []
    assert _LEAVES == {"pod", "dev_fleet"}
    assert not _worth_parsing("from unittest import mock\nmock.patch.object(json, 'x')\n")
    assert not _worth_parsing("from kiro_crew.pod import runtime as rt\n")


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
