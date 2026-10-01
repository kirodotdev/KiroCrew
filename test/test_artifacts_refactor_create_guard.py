"""No test patches a name the artifact facade forwards with ``create=True``.

The facade forwards a delete of a forwarded name to the module that owns it.
``mock.patch`` restores a name the facade does not hold by deleting it and then,
finding it gone, writing its original back -- unless ``create`` is true, in which
case it skips that write and the owner loses the name for every later caller in the
worker. So such a patch is refused here, by a scan of every test tree, rather than
emulated in the facade.

The scan resolves each call from the syntax tree: which callable is ``mock.patch``
under any import alias, and which module and name a target means, through aliases,
module-name constants, f-strings and concatenation. A target it cannot resolve is
reported as ``<dynamic>`` rather than dropped, because a guard that skips what it
cannot read passes exactly the patch it exists to refuse. It has two blind spots: a
call it cannot resolve to ``mock.patch`` -- a local helper that wraps it, say -- is
not examined at all, and neither is a file that never spells ``artifacts``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from kiro_crew import artifacts as art_mod

_FACADE = art_mod.__name__
_DYNAMIC = "<dynamic>"

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

#: How strongly an assignment's answer counts when a name is bound more than once:
#: a module path, then a value that could be one, then a value that cannot. A dotted
#: path ranks above all three.
_RANK = {None: -1, "<local>": 0, "<unknown>": 1}


def _rank(bound: str | None) -> int:
    """Which of two bindings of one name the scan keeps: the facade beats any other."""
    return 4 if bound == _FACADE else _RANK.get(bound, 3)


def _text_rank(text: str | None) -> int:
    """Which of two string bindings of one name the scan keeps: one spelling the
    facade's path wins over any other, and otherwise the first binding stands."""
    if text is None:
        return 0
    return 2 if text == _FACADE or text.startswith(_FACADE + ".") else 1


#: Deliberate ``create=True`` premises, as ``(path relative to the repository, test
#: function)``. The raw scan must equal this set exactly.
_ALLOWED: frozenset[tuple[str, str]] = frozenset()


class _Scope:
    """What each name in one file means, resolved to a fixed point.

    A name maps to the dotted path of the module or object it is bound to, to
    ``"<local>"`` for a binding that cannot be a module (a constant, a function or
    class defined here), to ``"<unknown>"`` for an assignment whose value cannot be
    evaluated (a call's result, say), or is absent when the file never binds it (a
    parameter, such as a fixture). A name bound more than once keeps the answer
    that reaches the facade, else the one that could. String constants are kept
    apart, for resolving patch targets spelled as text, and one spelling the
    facade's path likewise wins.
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
                if text is not None and _text_rank(text) > _text_rank(self.strings.get(name)):
                    self.strings[name] = text
                    changed = True
                bound = self.module(value) or "<unknown>"
                if _rank(bound) > _rank(self.names.get(name)):
                    self.names[name] = bound
                    changed = True
            if not changed:
                break

    def module(self, node: ast.expr) -> str | None:
        """The dotted path *node* names, ``"<local>"`` for another binding, or None."""
        if isinstance(node, ast.Name):
            bound = self.names.get(node.id)
            return None if bound == "<unknown>" else bound
        if isinstance(node, ast.Attribute):
            base = self.module(node.value)
            if base is None or base == "<local>":
                return base
            return f"{base}.{node.attr}"
        if isinstance(node, ast.Call):
            callee = self.module(node.func)
            if callee == "importlib.import_module" and node.args:
                return self.text(node.args[0])
            return None
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
    """False only when ``create`` is absent or the literal ``False``. A ``*args``
    positional at or before ``create``'s slot may carry it, so it counts as present."""
    position = _CREATE_POSITION[form]
    if any(isinstance(arg, ast.Starred) for arg in call.args[: position + 1]):
        return True
    create = _argument(call, position, "create")
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
        return [name] if owner == _FACADE else []
    target = _argument(call, 0, "target")
    if target is None:
        return [_DYNAMIC]
    module = scope.module(target) if not isinstance(target, ast.Constant) else None
    if module is None and isinstance(target, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        module = scope.text(target)
    if module is None:
        return [_DYNAMIC]
    if module != _FACADE:
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
                    if name == _DYNAMIC or name in art_mod._EXPORTS:
                        found.append((node.lineno, function, name))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


_IMPORT_ART = "from kiro_crew import artifacts as art\n"
_FROM_MOCK = "from unittest import mock\n"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # (1) the patch callable under any import alias, and as a decorator
        (
            'from unittest.mock import patch as _p\n_p("' + _FACADE + '.MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        ('from elsewhere import patch as _p\n_p("' + _FACADE + '.MAX_TAGS", create=True)', []),
        (
            _IMPORT_ART
            + 'import unittest.mock as um\num.patch.object(art, "MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _IMPORT_ART
            + "from unittest import mock as m\nm.patch.multiple(art, MAX_TAGS=1, create=True)",
            ["MAX_TAGS"],
        ),
        (
            _IMPORT_ART + "from unittest.mock import patch\n"
            '@patch.object(art, "MAX_TAGS", create=True)\ndef test_x(): pass',
            ["MAX_TAGS"],
        ),
        (
            _IMPORT_ART + 'def test_x(mocker): mocker.patch.object(art, "MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (_IMPORT_ART + 'def test_x(other): other.patch.object(art, "MAX_TAGS", create=True)', []),
        # (2) targets spelled as f-strings, module-name constants and concatenation
        (
            _IMPORT_ART + _FROM_MOCK + 'mock.patch(f"{art.__name__}.MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK + f'_MOD = "{_FACADE}"\nmock.patch(f"{{_MOD}}.MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK + f'_MOD = "{_FACADE}"\nmock.patch(_MOD + ".MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (_FROM_MOCK + '_MOD = "kiro_crew.other"\nmock.patch(f"{_MOD}.MAX_TAGS", create=True)', []),
        (_FROM_MOCK + 'import os\nmock.patch(f"{os.__name__}.MAX_TAGS", create=True)', []),
        # (3) the target= and attribute= keyword forms
        (
            _IMPORT_ART
            + _FROM_MOCK
            + 'mock.patch.object(target=art, attribute="MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (_FROM_MOCK + f'mock.patch(target="{_FACADE}.MAX_TAGS", create=True)', ["MAX_TAGS"]),
        (
            _IMPORT_ART
            + _FROM_MOCK
            + 'mock.patch.object(target=art, attribute="MAX_VERSIONS", create=True)',
            [],
        ),
        # (4) any create that is not the literal False, by keyword or position
        (_IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS", create=1)', ["MAX_TAGS"]),
        (
            _IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS", None, None, flag)',
            ["MAX_TAGS"],
        ),
        (_IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS", *flags)', ["MAX_TAGS"]),
        (_IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS", create=False)', []),
        (_IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS")', []),
        # (5) what cannot be resolved is reported, never dropped
        (_IMPORT_ART + _FROM_MOCK + "mock.patch.object(art, name, create=True)", [_DYNAMIC]),
        (_FROM_MOCK + 'def test_x(module): mock.patch(f"{module}.x", create=True)', [_DYNAMIC]),
        (_IMPORT_ART + _FROM_MOCK + "mock.patch.multiple(art, create=True, **names)", [_DYNAMIC]),
        (_FROM_MOCK + 'def test_x(obj): mock.patch.object(obj, "x", create=True)', [_DYNAMIC]),
        (_FROM_MOCK + 'import json\nmock.patch.object(json, "x", create=True)', []),
        (_IMPORT_ART + _FROM_MOCK + "mock.patch.object(*call_args)", [_DYNAMIC]),
        # (6) aliases bound by assignment, resolved to a fixed point
        (
            _IMPORT_ART
            + _FROM_MOCK
            + 'a = art\nb = a\nmock.patch.object(b, "MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK + f'import importlib\nh = importlib.import_module("{_FACADE}")\n'
            'mock.patch.object(h, "MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK + 'import importlib\nh = importlib.import_module("json")\n'
            'mock.patch.object(h, "MAX_TAGS", create=True)',
            [],
        ),
        (
            _FROM_MOCK
            + 'def _load(): pass\nh = _load()\nmock.patch.object(h, "MAX_TAGS", create=True)',
            [_DYNAMIC],
        ),
        (_FROM_MOCK + 'class H: pass\nmock.patch.object(H, "MAX_TAGS", create=True)', []),
        # (7) a name bound more than once keeps the binding that reaches the facade
        (
            _FROM_MOCK + "import importlib\n"
            'def a(): m = importlib.import_module("kiro_crew.artifact_store.rules")\n'
            f'def b(): m = importlib.import_module("{_FACADE}")\n'
            'mock.patch.object(m, "MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK
            + f'import importlib\nimport json as m\nm = importlib.import_module("{_FACADE}")\n'
            'mock.patch.object(m, "MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK + f'_MOD = "kiro_crew.other"\n_MOD = "{_FACADE}"\n'
            'mock.patch(f"{_MOD}.MAX_TAGS", create=True)',
            ["MAX_TAGS"],
        ),
        (
            _FROM_MOCK + "import importlib\nimport json as m\n"
            'm = importlib.import_module("kiro_crew.artifact_store.rules")\n'
            'mock.patch.object(m, "MAX_TAGS", create=True)',
            [],
        ),
    ],
)
def test_the_detector_answers_both_ways(source: str, expected: list[str]) -> None:
    assert [name for _line, _function, name in _create_patches(ast.parse(source))] == expected


def _worth_scanning(text: str) -> bool:
    """Whether a file can reach the facade at all; ``create`` is judged from the AST."""
    return "artifacts" in text


@pytest.mark.parametrize(
    ("text", "worth"),
    [
        (_IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS", None, None, flag)', True),
        (_IMPORT_ART + _FROM_MOCK + 'mock.patch.object(art, "MAX_TAGS", **options)', True),
        (_FROM_MOCK + 'mock.patch.object(other, "x", create=True)', False),
    ],
    ids=["positional-create", "splatted-create", "never-names-the-facade"],
)
def test_the_scan_reads_every_file_that_names_the_facade(text: str, worth: bool) -> None:
    assert _worth_scanning(text) is worth


def test_no_test_patches_a_forwarded_name_that_it_may_create() -> None:
    repo = Path(inspect.getfile(art_mod)).parents[2]
    roots = [repo / "test", *sorted((repo / "src" / "kiro_crew").rglob("tests"))]
    scanned = 0
    hits: dict[tuple[str, str], list[str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            scanned += 1
            if not _worth_scanning(text):
                continue
            relative = path.relative_to(repo).as_posix()
            for line, function, name in _create_patches(ast.parse(text)):
                hits.setdefault((relative, function), []).append(f"line {line}: {name}")
    assert scanned > 100, f"the scan read only {scanned} test files, so it measured nothing"
    assert set(hits) == _ALLOWED, hits
