"""No test patches a name ``kiro_crew.cli_doctor`` forwards with ``create=True``.

``kiro_crew.cli_doctor`` forwards a delete of a moved name to the family that owns
it. ``mock.patch`` restores a name the facade does not hold by deleting it and then,
finding it gone, writing its original back -- unless ``create`` is true, in which case
it skips that write and the family loses the name for every later caller in the
worker. So such a patch is refused here, by a scan of every test tree, rather than
emulated in the facade.

The scan resolves each call from the syntax tree: which callable is ``mock.patch``
under any import alias, and which module and name a target means, through aliases,
module-name constants, f-strings and concatenation. The facade is reached under its
own name and as the ``cli_doctor`` each family binds it to at module level, so
``patch.object(agents.cli_doctor, ...)`` is the same target. A target it cannot spell,
or a name the file never binds (a parameter, such as a fixture), is reported as
``<dynamic>`` rather than dropped, because a guard that skips what it cannot read
passes exactly the patch it exists to refuse. A name bound from any other call or
subscript is read as a local value, not the facade.
"""

from __future__ import annotations

import ast
import importlib
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest
from stale_package_attribute import package_attribute_replaced

from kiro_crew import cli_doctor

_FACADE = cli_doctor.__name__
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
_PREMISE = "test_the_premise_create_true_through_the_facade_unbinds_the_family"

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
    """The facade's dotted name, plus ``<module>.<name>`` for each src module that
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


class _Scope:
    """What each name in one file means, resolved to a fixed point.

    A name maps to the dotted path of the module or object it is bound to, to
    ``"<local>"`` for a binding that is not a module path, or is absent when the file
    never binds it (a parameter, such as a fixture). String constants are kept
    apart, for resolving patch targets spelled as text.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.names: dict[str, str] = {"mocker": "mocker"}
        self.strings: dict[str, str] = {}
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
                self.names.setdefault(node.name, "<local>")
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
                bound = self.module(value) or "<local>"
                if self.names.get(name) in (None, "<local>") and bound != self.names.get(name):
                    self.names[name] = bound
                    changed = True
            if not changed:
                break

    def module(self, node: ast.expr) -> str | None:
        """The dotted path *node* names, ``"<local>"`` for another binding, or None."""
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.module(node.value)
            if base is None or base == "<local>":
                return base
            return f"{base}.{node.attr}"
        if isinstance(node, ast.Call):
            callee = self.module(node.func)
            if callee == "importlib.import_module" and node.args:
                return self.text(node.args[0])
            return "<local>" if callee is not None else None
        if isinstance(node, ast.Subscript) and self.module(node.value) == "sys.modules":
            return self.text(node.slice)
        if isinstance(node, ast.Constant):
            return "<local>"
        return None

    def text(self, node: ast.expr) -> str | None:
        """The string *node* spells, or None when it cannot be known from the file."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return self.strings.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "__name__":
            base = self.module(node.value)
            return base if base not in (None, "<local>") else None
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


def _create_patches(tree: ast.Module) -> list[tuple[int, str, str]]:
    """``(line, test function, name)`` for each patch that may create a forwarded name."""
    scope = _Scope(tree)
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Call):
            form = _PATCH_FORMS.get(scope.module(node.func) or "")
            if form is not None and _may_create(node, form):
                for name in _patched_names(node, form, scope):
                    if name == _DYNAMIC or name in cli_doctor._EXPORTS:
                        found.append((node.lineno, function, name))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


_IMPORT_CD = "from kiro_crew import cli_doctor as cd\n"
_FROM_MOCK = "from unittest import mock\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # (1) the patch callable under any import alias, and as a decorator
        (
            'from unittest.mock import patch as _p\n_p("'
            + _FACADE
            + '._doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            'from elsewhere import patch as _p\n_p("' + _FACADE + '._doctor_sandbox", create=True)',
            [],
        ),
        (
            _IMPORT_CD
            + 'import unittest.mock as um\num.patch.object(cd, "_safe_display", create=True)',
            ["_safe_display"],
        ),
        (
            _IMPORT_CD
            + "from unittest import mock as m\nm.patch.multiple(cd, _swap_total_kib=1, create=True)",
            ["_swap_total_kib"],
        ),
        (
            _IMPORT_CD + "from unittest.mock import patch\n"
            '@patch.object(cd, "_doctor_sandbox", create=True)\ndef test_x(): pass',
            ["_doctor_sandbox"],
        ),
        (
            _IMPORT_CD
            + 'def test_x(mocker): mocker.patch.object(cd, "_doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _IMPORT_CD
            + 'def test_x(other): other.patch.object(cd, "_doctor_sandbox", create=True)',
            [],
        ),
        # (2) targets spelled as f-strings, module-name constants and concatenation
        (
            _IMPORT_CD + _FROM_MOCK + 'mock.patch(f"{cd.__name__}._doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK
            + f'_MOD = "{_FACADE}"\nmock.patch(f"{{_MOD}}._doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK + f'_MOD = "{_FACADE}"\nmock.patch(_MOD + "._doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK
            + '_MOD = "kiro_crew.other"\nmock.patch(f"{_MOD}._doctor_sandbox", create=True)',
            [],
        ),
        (_FROM_MOCK + 'import os\nmock.patch(f"{os.__name__}._doctor_sandbox", create=True)', []),
        # (3) the target= and attribute= keyword forms
        (
            _IMPORT_CD
            + _FROM_MOCK
            + 'mock.patch.object(target=cd, attribute="_doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK + f'mock.patch(target="{_FACADE}._doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _IMPORT_CD
            + _FROM_MOCK
            + 'mock.patch.object(target=cd, attribute="_a_new_name", create=True)',
            [],
        ),
        # (4) any create that is not the literal False, by keyword or position
        (
            _IMPORT_CD + _FROM_MOCK + 'mock.patch.object(cd, "_doctor_sandbox", create=1)',
            ["_doctor_sandbox"],
        ),
        (
            _IMPORT_CD + _FROM_MOCK + 'mock.patch.object(cd, "_doctor_sandbox", None, None, flag)',
            ["_doctor_sandbox"],
        ),
        (_IMPORT_CD + _FROM_MOCK + 'mock.patch.object(cd, "_doctor_sandbox", create=False)', []),
        (_IMPORT_CD + _FROM_MOCK + 'mock.patch.object(cd, "_doctor_sandbox")', []),
        # a name the facade binds itself is restored by assignment, create or not
        (_IMPORT_CD + _FROM_MOCK + 'mock.patch.object(cd, "config_dir", create=True)', []),
        # (5) what cannot be resolved is reported, never dropped
        (_IMPORT_CD + _FROM_MOCK + "mock.patch.object(cd, name, create=True)", [_DYNAMIC]),
        (_FROM_MOCK + 'def test_x(module): mock.patch(f"{module}.x", create=True)', [_DYNAMIC]),
        (_IMPORT_CD + _FROM_MOCK + "mock.patch.multiple(cd, create=True, **names)", [_DYNAMIC]),
        (_FROM_MOCK + 'def test_x(obj): mock.patch.object(obj, "x", create=True)', [_DYNAMIC]),
        (_FROM_MOCK + 'import json\nmock.patch.object(json, "x", create=True)', []),
        # (6) aliases bound by assignment, resolved to a fixed point
        (
            _IMPORT_CD
            + _FROM_MOCK
            + 'a = cd\nb = a\nmock.patch.object(b, "_doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK + f'import importlib\nh = importlib.import_module("{_FACADE}")\n'
            'mock.patch.object(h, "_doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK + 'import importlib\nh = importlib.import_module("json")\n'
            'mock.patch.object(h, "_doctor_sandbox", create=True)',
            [],
        ),
        (
            _FROM_MOCK + f'import sys\nh = sys.modules["{_FACADE}"]\n'
            'mock.patch.object(h, "_doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK + 'import sys\nh = sys.modules["json"]\n'
            'mock.patch.object(h, "_doctor_sandbox", create=True)',
            [],
        ),
        # the facade reached as the ``cli_doctor`` a family binds it to
        (
            "from kiro_crew.doctor_checks import agents\n"
            + _FROM_MOCK
            + 'mock.patch.object(agents.cli_doctor, "_doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            _FROM_MOCK
            + 'mock.patch("kiro_crew.doctor_checks.agents.cli_doctor._doctor_sandbox", create=True)',
            ["_doctor_sandbox"],
        ),
        (
            "from kiro_crew.doctor_checks import agents\n"
            + _FROM_MOCK
            + 'mock.patch.object(agents, "_doctor_sandbox", create=True)',
            [],
        ),
    ],
)
def test_the_detector_answers_both_ways(source: str, expected: list[str]) -> None:
    assert [name for _line, _function, name in _create_patches(ast.parse(source))] == expected


def test_the_facade_is_found_under_every_name_src_binds_it_to() -> None:
    families = {
        f"kiro_crew.doctor_checks.{family}.cli_doctor"
        for family in ("agents", "confinement", "install", "resources", "services")
    }
    assert {_FACADE} | families <= _FACADE_PATHS


@pytest.mark.parametrize("split", [False, True], ids=["as-imported", "package-attribute-stale"])
def test_the_premise_create_true_through_the_facade_unbinds_the_family(split: bool) -> None:
    """Why the scan exists: the one ``create=True`` patch it allows, run for real.

    The owner is read from ``sys.modules``, where the facade writes. Its package
    attribute can name another copy once an earlier test in the worker imports it
    fresh, so reading the attribute would make this check depend on test order.
    """
    owner = "kiro_crew.doctor_checks.render"
    with package_attribute_replaced(owner) if split else nullcontext():
        render = importlib.import_module(owner)
        original = vars(render)["_safe_display"]
        try:
            with mock.patch.object(cli_doctor, "_safe_display", create=True):
                pass
            assert "_safe_display" not in vars(render)
        finally:
            vars(render)["_safe_display"] = original


def test_no_test_patches_a_forwarded_name_that_it_may_create() -> None:
    roots = [_REPO / "test", *sorted(_SRC.rglob("tests"))]
    # Every spelling of the facade -- its import, its dotted name, a family's
    # ``cli_doctor`` attribute -- carries its leaf name, so a file without it cannot
    # reach the facade and is not parsed.
    needles = {_FACADE.rpartition(".")[2]}
    scanned = 0
    hits: dict[tuple[str, str], list[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            scanned += 1
            if "create" not in text or not any(needle in text for needle in needles):
                continue
            relative = path.relative_to(_REPO).as_posix()
            for line, function, name in _create_patches(ast.parse(text)):
                hits.setdefault((relative, function), []).append(f"line {line}: {name}")
    assert scanned > 100, f"the scan read only {scanned} test files, so it measured nothing"
    assert set(hits) == _ALLOWED, hits
    assert [len(hits[key]) for key in sorted(_ALLOWED)] == [1] * len(_ALLOWED), hits
