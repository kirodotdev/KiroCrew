"""A test that fills the stack to the recursion limit carries ``at_recursion_limit``.

The CI lanes that measure with coverage.py's ``sysmon`` core deselect that marker
and run its tests in a step of their own on the C tracer
(``scripts/ci/coverage_core.py`` says why: at that depth a ``sys.monitoring``
callback has no frame left, and a ``with`` block can end without its exit). A
test with the shape and without the marker would stay in the ``sysmon`` run and
could hang it, so this gate reads every test source and names the ones that have
the shape.

The shape is read from the syntax tree, with no list of files kept here: a test
that reaches, through the functions of its own module, both a call that reads or
sets the interpreter's recursion limit and a function that calls itself. Reading
the limit to size an input (a deeply nested document handed to product code) is
not the shape, because nothing in the test recurses; recursing without reading
the limit is not it either, because the depth is then a constant the test chose.
"""

from __future__ import annotations

import ast
import textwrap

import pytest

from scripts.ci import coverage_core

MARKER = coverage_core.MARKER
LIMIT_CALLS = {"getrecursionlimit", "setrecursionlimit"}


def _called(node: ast.AST) -> tuple[set[str], bool]:
    """The module's own names this node calls, and whether it reads or sets the limit.

    ``f()`` and ``self.f()`` name a function of this module. Any other ``x.f()``
    is a call on an object the test holds, which says nothing about the module's
    ``f``; only the two interpreter calls are read from it.
    """
    names, limit = set(), False
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        target = call.func
        if isinstance(target, ast.Name):
            names.add(target.id)
            limit = limit or target.id in LIMIT_CALLS
        elif isinstance(target, ast.Attribute):
            limit = limit or target.attr in LIMIT_CALLS
            if isinstance(target.value, ast.Name) and target.value.id in ("self", "cls"):
                names.add(target.attr)
    return names, limit


def _marks(decorators: list[ast.expr]) -> set[str]:
    """Marker names in a decorator list: ``pytest.mark.x`` and ``pytest.mark.x(...)``."""
    found = set()
    for decorator in decorators:
        node = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _module_marks(tree: ast.Module) -> set[str]:
    """Markers a module-level ``pytestmark`` applies to every test in the file."""
    found = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            value = statement.value
            items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            found |= _marks(list(items))
    return found


def stack_filling_tests(source: str) -> dict[str, bool]:
    """Each test with the shape, by name, and whether it carries the marker."""
    tree = ast.parse(source)
    functions: dict[str, ast.AST] = {}
    marks: dict[str, set[str]] = {}
    module_marks = _module_marks(tree)

    def collect(body: list[ast.stmt], inherited: set[str]) -> None:
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions[statement.name] = statement
                marks[statement.name] = inherited | _marks(statement.decorator_list)
            elif isinstance(statement, ast.ClassDef):
                collect(statement.body, inherited | _marks(statement.decorator_list))

    collect(tree.body, module_marks)
    read = {name: _called(node) for name, node in functions.items()}
    calls = {name: called for name, (called, _) in read.items()}
    reads_limit = {name for name, (_, limit) in read.items() if limit}
    recurses = {name for name, called in calls.items() if name in called}

    def reach(start: str) -> set[str]:
        seen, pending = {start}, [start]
        while pending:
            for callee in calls[pending.pop()] & functions.keys():
                if callee not in seen:
                    seen.add(callee)
                    pending.append(callee)
        return seen

    shaped = {}
    for name in functions:
        if name.startswith("test"):
            reached = reach(name)
            if reached & reads_limit and reached & recurses:
                shaped[name] = MARKER in marks[name]
    return shaped


def _source(body: str, mark: str = "") -> str:
    """The sample with its ``@MARK`` line turned into a marker, or dropped."""
    lines = []
    for line in textwrap.dedent(body).splitlines():
        if line.strip() != "@MARK":
            lines.append(line)
        elif mark:
            lines.append(line.replace("@MARK", f"@pytest.mark.{mark}"))
    return "\n".join(lines) + "\n"


FILLS_DIRECTLY = """
    import sys
    import pytest

    def _down(depth):
        if depth:
            _down(depth - 1)

    @MARK
    def test_fills():
        _down(sys.getrecursionlimit() - 50)
"""
FILLS_THROUGH_HELPERS = """
    import sys
    import pytest

    def _room():
        return sys.getrecursionlimit() - 40

    def _down(depth):
        return _down(depth - 1) if depth else 0

    def _run():
        _down(_room())

    class TestDeep:
        @MARK
        def test_fills(self):
            _run()
"""
LOWERS_THE_LIMIT = """
    import sys
    import pytest

    def _down(depth):
        return _down(depth - 1) if depth else 0

    @MARK
    def test_fills():
        sys.setrecursionlimit(200)
        _down(190)
"""
SIZES_AN_INPUT = """
    import sys

    def test_deep_document_is_refused():
        depth = sys.getrecursionlimit() * 3
        document = "[" * depth + "]" * depth
        assert len(document) == 2 * depth
"""
RECURSES_TO_A_CONSTANT = """
    def _down(depth):
        return _down(depth - 1) if depth else 0

    def test_shallow():
        assert _down(20) == 0
"""
CALLS_A_NAMESAKE_METHOD = """
    import sys

    def add(depth):
        return add(depth - 1) if depth else 0

    def test_builds_a_long_chain(table):
        for pid in range(sys.getrecursionlimit() + 200):
            table.add(pid)
"""
TWO_TESTS_ONE_SHAPED = """
    import sys

    def _down(depth):
        return _down(depth - 1) if depth else 0

    def test_reads_only():
        assert sys.getrecursionlimit() > 100

    def test_recurses_only():
        assert _down(5) == 0
"""


@pytest.mark.parametrize("body", [FILLS_DIRECTLY, FILLS_THROUGH_HELPERS, LOWERS_THE_LIMIT])
def test_the_shape_is_found_and_the_marker_is_read_where_it_is_written(body):
    assert stack_filling_tests(_source(body)) == {"test_fills": False}
    assert stack_filling_tests(_source(body, MARKER)) == {"test_fills": True}
    assert stack_filling_tests(_source(body, "slow")) == {"test_fills": False}


@pytest.mark.parametrize("where", ["module", "class"])
def test_a_marker_on_the_module_or_the_class_covers_its_tests(where):
    source = _source(FILLS_THROUGH_HELPERS)
    mark = f"pytest.mark.{MARKER}"
    if where == "module":
        source += f"\npytestmark = [pytest.mark.slow, {mark}]\n"
    else:
        source = source.replace("class TestDeep:", f"@{mark}\nclass TestDeep:")
    assert stack_filling_tests(source) == {"test_fills": True}


@pytest.mark.parametrize(
    "body", [SIZES_AN_INPUT, RECURSES_TO_A_CONSTANT, CALLS_A_NAMESAKE_METHOD, TWO_TESTS_ONE_SHAPED]
)
def test_reading_the_limit_or_recursing_alone_is_not_the_shape(body):
    assert stack_filling_tests(_source(body)) == {}


def test_every_test_that_fills_the_stack_to_the_limit_carries_the_marker():
    unmarked, marked_files = [], set()
    for name, text in coverage_core.test_sources():
        if "recursionlimit" not in text:
            continue  # the shape cannot exist without one of the two calls
        for test, marked in stack_filling_tests(text).items():
            if marked:
                marked_files.add(name)
            else:
                unmarked.append(f"{name}::{test}")
    assert not unmarked, (
        f"add @pytest.mark.{MARKER}: these tests fill the stack to the recursion limit, "
        f"and the sysmon lanes would run them: {unmarked}"
    )
    # The lanes hand pytest the files that name the marker; every marked test is in one.
    assert marked_files <= set(coverage_core.marked_files())
