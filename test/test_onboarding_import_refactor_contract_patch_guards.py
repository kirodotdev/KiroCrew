"""The onboarding facade's two AST guards over the test suite.

Both read every file under ``test/`` and every ``tests/`` package under ``src/`` whose
text contains ``onboarding_import``:

* no test passes ``mock.patch`` a ``create`` other than the literal ``False`` for a
  mirrored name on the facade, the one spelling whose undo loses the owner's binding;
  the scan must equal an explicit ``(path, test)`` allowlist, which is empty;
* a name a test rebinds on the facade has one binding every reader reads: no engine
  module but the facade, or a mirrored name's owner, also binds it.

Each rule is pinned on a synthetic source it must flag and one it must ignore. The rest
of the facade's compatibility contract is in ``test_onboarding_import_refactor_contract.py``.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
from collections import Counter
from collections.abc import Collection
from pathlib import Path

import pytest
import source_corpus
from onboarding_import_refactor_contract_helpers import _ENGINE_MODULES

from kiro_crew import onboarding_import

# Keep apply_import's MCP sidecar lock off the real ~/.kiro/settings (see the
# shared fixture in test/conftest.py).
pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")


# ── A ``create`` on a mirrored name is refused ─────────────────────────────────
#
# ``mock.patch`` finds a mirrored name through the facade's ``__getattr__``, so it
# treats it as not local and restores it on exit by DELETING it, which the facade
# forwards to the owner, and then setting it back -- unless ``create`` is truthy,
# in which case it skips the set because it believes it created the name. The owner
# is left without its cap or helper for the rest of the worker. The forwarding
# cannot tell that delete from any other, so the guard below refuses, in every file
# under ``test/`` and every ``tests/`` package under ``src/`` that names the facade,
# each ``mock.patch`` spelling it resolves (listed on ``mirrored_create_patches``)
# whose target is a mirrored name on the facade and whose ``create`` is anything but
# the literal ``False``. The one exemption is ``_CREATE_PATCH_ALLOWLIST``, which the
# scan must equal exactly.

_FACADE = "kiro_crew.onboarding_import"

#: Reported in place of a mirrored name the source only computes at runtime.
_DYNAMIC = "<dynamic>"

#: Stands in for the part of a string the source only computes at runtime.
_HOLE = "\x00"

#: Where ``create`` sits positionally in each spelling. ``patch.dict`` has no such
#: parameter, so only a ``create=`` keyword counts there.
_CREATE_POSITION = {"": 3, "object": 4, "multiple": 2}

_MOCK_MODULES = ("unittest.mock", "mock")


@dataclasses.dataclass
class _Bindings:
    """What the names in one file are bound to, read off its import and assignment nodes."""

    roots: set[str] = dataclasses.field(default_factory=set)
    patchers: set[str] = dataclasses.field(default_factory=set)
    #: A name bound to ``patch.object``, ``patch.dict`` or ``patch.multiple`` -> which.
    methods: dict[str, str] = dataclasses.field(default_factory=dict)
    mock_modules: set[str] = dataclasses.field(default_factory=set)
    importlibs: set[str] = dataclasses.field(default_factory=set)
    importers: set[str] = dataclasses.field(default_factory=set)
    facades: set[str] = dataclasses.field(default_factory=set)
    helpers: set[str] = dataclasses.field(default_factory=set)
    strings: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _Hit:
    """One refused patch: its line, its enclosing test, and the name it addresses."""

    line: int
    scope: str
    name: str


def _dotted(node: ast.expr | None, bindings: _Bindings) -> str | None:
    """``a.b.c`` for an attribute chain rooted at a name a plain ``import`` bound."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bindings.roots:
        return None
    return ".".join([node.id, *reversed(parts)])


def _static_text(node: ast.expr | None, bindings: _Bindings) -> str:
    """The text a string expression evaluates to, with ``_HOLE`` for each runtime part.

    Resolves a literal, a module-level string constant, the facade's ``__name__``, an
    f-string and a ``+`` concatenation, each part by its own node.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _HOLE
    if isinstance(node, ast.Name):
        return bindings.strings.get(node.id, _HOLE)
    if isinstance(node, ast.Attribute) and node.attr == "__name__":
        return _FACADE if _is_facade(node.value, bindings) else _HOLE
    if isinstance(node, ast.JoinedStr):
        return "".join(_fstring_part(part, bindings) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_text(node.left, bindings) + _static_text(node.right, bindings)
    return _HOLE


def _fstring_part(part: ast.expr, bindings: _Bindings) -> str:
    if isinstance(part, ast.Constant) and isinstance(part.value, str):
        return part.value
    if isinstance(part, ast.FormattedValue) and part.conversion == -1 and not part.format_spec:
        return _static_text(part.value, bindings)
    return _HOLE


def _is_import_module(node: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(node, ast.Name):
        return node.id in bindings.importers
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "import_module"
        and isinstance(node.value, ast.Name)
        and node.value.id in bindings.importlibs
    ):
        return True
    return _dotted(node, bindings) == "importlib.import_module"


def _is_loaded_module(node: ast.expr | None, module: str, bindings: _Bindings) -> bool:
    """An ``import_module`` result or ``sys.modules`` entry for the module named *module*."""
    if isinstance(node, ast.Call):
        return (
            _is_import_module(node.func, bindings)
            and bool(node.args)
            and _static_text(node.args[0], bindings) == module
        )
    if isinstance(node, ast.Subscript):
        return (
            _dotted(node.value, bindings) == "sys.modules"
            and _static_text(node.slice, bindings) == module
        )
    return False


def _is_mock_module(node: ast.expr | None, bindings: _Bindings) -> bool:
    """Whether *node* evaluates to ``unittest.mock`` or the ``mock`` backport."""
    if isinstance(node, ast.Name):
        return node.id in bindings.mock_modules
    if isinstance(node, ast.Attribute):
        return _dotted(node, bindings) == "unittest.mock"
    return any(_is_loaded_module(node, module, bindings) for module in _MOCK_MODULES)


def _is_facade(node: ast.expr | None, bindings: _Bindings) -> bool:
    """Whether *node* evaluates to the facade module, read off the node's own shape."""
    if isinstance(node, ast.Name):
        return node.id in bindings.facades
    if isinstance(node, ast.Attribute):
        return _dotted(node, bindings) == _FACADE
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in bindings.helpers:
            return True
    return _is_loaded_module(node, _FACADE, bindings)


def _bindings(tree: ast.Module) -> _Bindings:
    """Read one file's bindings: imports, module-level strings, then assigned aliases.

    Assigned aliases are resolved to a fixed point, in whatever order the file binds
    them. A name assigned the facade, ``unittest.mock`` / ``mock``, ``patch``, or
    ``patch.object`` / ``.dict`` / ``.multiple`` -- directly, through an earlier
    alias, as an ``import_module`` result or a ``sys.modules`` entry -- is bound to
    it, and so is a function that returns the facade.
    """
    bindings = _Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    bindings.roots.add(alias.name.split(".")[0])
                    if alias.name == "mock":
                        bindings.mock_modules.add("mock")
                elif alias.name in _MOCK_MODULES:
                    bindings.mock_modules.add(alias.asname)
                elif alias.name == "importlib":
                    bindings.importlibs.add(alias.asname)
                elif alias.name == _FACADE:
                    bindings.facades.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module in _MOCK_MODULES and alias.name in ("patch", "*"):
                    bindings.patchers.add("patch" if alias.name == "*" else bound)
                elif node.module == "unittest" and alias.name == "mock":
                    bindings.mock_modules.add(bound)
                elif node.module == "importlib" and alias.name == "import_module":
                    bindings.importers.add(bound)
                elif node.module == "kiro_crew" and alias.name == "onboarding_import":
                    bindings.facades.add(bound)

    # Module-level string constants, each bound exactly once, resolved in order.
    assignments: list[tuple[str, ast.expr]] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            assignments.extend(
                (target.id, statement.value)
                for target in statement.targets
                if isinstance(target, ast.Name)
            )
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            assignments.append((statement.target.id, statement.value))
    bound_once = {name for name, count in Counter(n for n, _v in assignments).items() if count == 1}
    for name, value in assignments:
        text = _static_text(value, bindings)
        if name in bound_once and _HOLE not in text:
            bindings.strings[name] = text

    def size() -> tuple[int, ...]:
        return (
            len(bindings.facades),
            len(bindings.helpers),
            len(bindings.mock_modules),
            len(bindings.patchers),
            len(bindings.methods),
            len(bindings.importers),
        )

    while True:
        known = size()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(
                    isinstance(inner, ast.Return) and _is_facade(inner.value, bindings)
                    for inner in ast.walk(node)
                ):
                    bindings.helpers.add(node.name)
                continue
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            method = _patch_method(node.value, bindings)
            if _is_facade(node.value, bindings):
                bindings.facades.update(names)
            elif _is_mock_module(node.value, bindings):
                bindings.mock_modules.update(names)
            elif method == "":
                bindings.patchers.update(names)
            elif method is not None:
                bindings.methods.update(dict.fromkeys(names, method))
            elif _is_import_module(node.value, bindings):
                bindings.importers.update(names)
        if size() == known:
            return bindings


def _is_patcher(node: ast.expr, bindings: _Bindings) -> bool:
    """``patch`` under any binding the file makes: a name, or ``<mock module>.patch``."""
    if isinstance(node, ast.Name):
        return node.id in bindings.patchers
    if isinstance(node, ast.Attribute) and node.attr == "patch":
        return _is_mock_module(node.value, bindings)
    return False


def _patch_method(func: ast.expr, bindings: _Bindings) -> str | None:
    """``""`` for ``patch(...)``, the method name for ``patch.object/dict/multiple``."""
    if isinstance(func, ast.Name) and func.id in bindings.methods:
        return bindings.methods[func.id]
    if _is_patcher(func, bindings):
        return ""
    if (
        isinstance(func, ast.Attribute)
        and func.attr in ("object", "dict", "multiple")
        and _is_patcher(func.value, bindings)
    ):
        return func.attr
    return None


def _argument(call: ast.Call, index: int | None, keyword: str) -> ast.expr | None:
    if (
        index is not None
        and len(call.args) > index
        and not any(isinstance(arg, ast.Starred) for arg in call.args[: index + 1])
    ):
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg == keyword), None)


def _create_may_be_set(call: ast.Call, method: str) -> bool:
    """Anything but a literal ``False``, a ``*`` / ``**`` splat included, may set it."""
    value = _argument(call, _CREATE_POSITION.get(method), "create")
    if value is None:
        return any(kw.arg is None for kw in call.keywords) or any(
            isinstance(arg, ast.Starred) for arg in call.args
        )
    return not (isinstance(value, ast.Constant) and value.value is False)


def _addressed(text: str, exports: Collection[str]) -> list[str]:
    """The mirrored name a dotted target addresses on the facade, if it names one."""
    if text.startswith(_FACADE + _HOLE):
        return [_DYNAMIC]
    prefix = f"{_FACADE}."
    if not text.startswith(prefix):
        return []
    name = text[len(prefix) :]
    if _HOLE in name:
        return [_DYNAMIC]
    return [name] if name in exports else []


def _patched_names(
    call: ast.Call, method: str, bindings: _Bindings, exports: Collection[str]
) -> list[str]:
    if method == "":
        return _addressed(_static_text(_argument(call, 0, "target"), bindings), exports)
    if method == "dict":
        in_dict = _argument(call, 0, "in_dict")
        if isinstance(in_dict, ast.Attribute) and _is_facade(in_dict.value, bindings):
            return [in_dict.attr] if in_dict.attr in exports else []
        return _addressed(_static_text(in_dict, bindings), exports)
    target = _argument(call, 0, "target")
    if not (_is_facade(target, bindings) or _static_text(target, bindings) == _FACADE):
        return []
    if method == "object":
        attribute = _static_text(_argument(call, 1, "attribute"), bindings)
        if _HOLE in attribute:
            return [_DYNAMIC]
        return [attribute] if attribute in exports else []
    names = [str(kw.arg) for kw in call.keywords if kw.arg in exports]
    if any(kw.arg is None for kw in call.keywords):
        names.append(_DYNAMIC)
    return names


class _CreatePatchFinder(ast.NodeVisitor):
    """Collect every refused patch, attributed to the def or class that encloses it.

    A decorator belongs to the def it decorates, so ``@patch(...)`` on a test is
    reported under that test's name.
    """

    def __init__(self, bindings: _Bindings, exports: Collection[str]) -> None:
        self.bindings = bindings
        self.exports = exports
        self.scope: list[str] = []
        self.hits: list[_Hit] = []

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def visit_Call(self, node: ast.Call) -> None:
        method = _patch_method(node.func, self.bindings)
        if method is not None and _create_may_be_set(node, method):
            scope = ".".join(self.scope) or "<module>"
            self.hits.extend(
                _Hit(node.lineno, scope, name)
                for name in _patched_names(node, method, self.bindings, self.exports)
            )
        self.generic_visit(node)


def mirrored_create_patches(tree: ast.Module, exports: Collection[str]) -> list[_Hit]:
    """Every ``mock.patch`` in *tree* that may pass ``create`` for a mirrored facade name.

    A hit is a call -- or a decorator -- to ``patch``, ``patch.object``,
    ``patch.dict`` or ``patch.multiple`` whose target resolves to the facade and
    names a mirrored seam, and whose ``create`` (keyword, positional or a splat) is
    anything but the literal ``False``. ``patch`` is recognised through
    ``unittest.mock`` or ``mock`` bound by an import, an assignment, an
    ``import_module`` result or a ``sys.modules`` entry, and as a name that a
    ``from ... import`` or an assignment binds to ``patch`` or one of its methods.
    The facade is recognised through a dotted string or a name bound to it (see
    ``_bindings``). A facade or ``patch`` received as a fixture or parameter, or
    fetched with ``getattr``, is not resolved. Once the target is the facade, a
    name the source only computes at runtime -- a variable, an unresolved f-string
    or concatenation, a ``**`` splat of ``patch.multiple`` names -- is reported as
    ``<dynamic>``.
    """
    finder = _CreatePatchFinder(_bindings(tree), exports)
    finder.visit(tree)
    return sorted(finder.hits, key=lambda hit: (hit.line, hit.name))


def _allowlist_mismatch(
    raw: set[tuple[str, str]], allowlist: frozenset[tuple[str, str]]
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """``(hits the allowlist does not name, allowlist entries nothing hits)``."""
    return raw - allowlist, set(allowlist) - raw


#: ``(path relative to the repository, qualified test name)`` of a deliberate
#: ``create`` premise test on a mirrored name. The scan must produce exactly this
#: set: an unlisted hit fails, and so does an entry nothing hits any more. Empty,
#: because no test needs one.
_CREATE_PATCH_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def _alias_case(import_line: str, patcher: str) -> str:
    """The three call spellings of one patch binding, each passing ``create=True``."""
    return (
        f"{import_line}\n"
        "from kiro_crew import onboarding_import\n"
        f'{patcher}("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        f'{patcher}.object(onboarding_import, "_toml", None, create=True)\n'
        f"{patcher}.multiple(onboarding_import, create=True, _MAX_DB_ROWS=1)\n"
    )


_ALIAS_HITS = [(3, "_MAX_FILES"), (4, "_toml"), (5, "_MAX_DB_ROWS")]

_MOCK = "from unittest import mock\nfrom kiro_crew import onboarding_import, onboarding_scan\n"

#: id -> (synthetic source, the ``(line, name)`` hits it must produce). Each rule the
#: guard enforces has a case it must flag and a case it must ignore.
_GUARD_CASES: dict[str, tuple[str, list[tuple[int, str]]]] = {
    # 1. Every binding of ``patch``: three aliases and the three plain spellings.
    "alias/flag: from unittest.mock import patch as X": (
        _alias_case("from unittest.mock import patch as X", "X"),
        _ALIAS_HITS,
    ),
    "alias/flag: from unittest import mock as X": (
        _alias_case("from unittest import mock as X", "X.patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: import unittest.mock as X": (
        _alias_case("import unittest.mock as X", "X.patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: from unittest.mock import patch": (
        _alias_case("from unittest.mock import patch", "patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: from unittest import mock": (
        _alias_case("from unittest import mock", "mock.patch"),
        _ALIAS_HITS,
    ),
    "alias/flag: import unittest.mock": (
        _alias_case("import unittest.mock", "unittest.mock.patch"),
        _ALIAS_HITS,
    ),
    "alias/ignore: a patch that is not unittest.mock's": (
        _alias_case("from requests import patch as X", "X"),
        [],
    ),
    "alias/ignore: a module named mock that is not unittest.mock": (
        _alias_case("import requests as mock", "mock.patch"),
        [],
    ),
    # 2. Decorators, attributed to the test they decorate.
    "decorator/flag": (
        "from unittest.mock import patch as X\n"
        + _MOCK
        + '@X("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        '@X.object(onboarding_import, "_toml", None, create=True)\n'
        "def test_decorated():\n    pass\n",
        [(4, "_MAX_FILES"), (5, "_toml")],
    ),
    "decorator/ignore: no create": (
        "from unittest.mock import patch as X\n"
        + _MOCK
        + '@X("kiro_crew.onboarding_import._MAX_FILES", 1)\n'
        '@X.object(onboarding_import, "_toml", None)\n'
        "def test_decorated():\n    pass\n",
        [],
    ),
    # 3. f-string targets built from the facade's __name__ or a module constant.
    "f-string/flag": (
        _MOCK + 'FACADE = "kiro_crew.onboarding_import"\n'
        'mock.patch(f"{onboarding_import.__name__}._MAX_FILES", 1, create=True)\n'
        'mock.patch(f"{FACADE}._is_link_like", None, create=True)\n',
        [(4, "_MAX_FILES"), (5, "_is_link_like")],
    ),
    "f-string/ignore: the owner's name, not the facade's": (
        _MOCK + 'OWNER = "kiro_crew.onboarding_scan"\n'
        'mock.patch(f"{onboarding_scan.__name__}._MAX_FILES", 1, create=True)\n'
        'mock.patch(f"{OWNER}._MAX_FILES", 1, create=True)\n',
        [],
    ),
    # 4. Concatenation onto a module constant.
    "concatenation/flag": (
        _MOCK + 'FACADE = "kiro_crew.onboarding_import"\n'
        'mock.patch(FACADE + "._MAX_FILES", 1, create=True)\n',
        [(4, "_MAX_FILES")],
    ),
    "concatenation/ignore: onto the owner's name": (
        _MOCK + 'OWNER = "kiro_crew.onboarding_scan"\n'
        'mock.patch(OWNER + "._MAX_FILES", 1, create=True)\n',
        [],
    ),
    # 5. The keyword spellings of target and attribute.
    "keywords/flag": (
        _MOCK + "mock.patch.object(\n"
        '    target=onboarding_import, attribute="_MAX_FILES", new=1, create=True\n'
        ")\n"
        'mock.patch(target="kiro_crew.onboarding_import._toml", new=None, create=True)\n'
        "mock.patch.multiple(target=onboarding_import, create=True, _MAX_DB_ROWS=1)\n",
        [(3, "_MAX_FILES"), (6, "_toml"), (7, "_MAX_DB_ROWS")],
    ),
    "keywords/ignore: the owner as target": (
        _MOCK + "mock.patch.object(\n"
        '    target=onboarding_scan, attribute="_MAX_FILES", new=1, create=True\n'
        ")\n",
        [],
    ),
    # 6. A create that is not a literal: a number, a name, None, positional, a splat.
    "create/flag: any value but the literal False": (
        _MOCK + 'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=1)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=flag)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=None)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, None, True)\n'
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1, **options)\n'
        "mock.patch.multiple(onboarding_import, create=flag, _MAX_DB_ROWS=1)\n",
        [
            (3, "_MAX_FILES"),
            (4, "_MAX_FILES"),
            (5, "_MAX_FILES"),
            (6, "_MAX_FILES"),
            (7, "_MAX_FILES"),
            (8, "_MAX_DB_ROWS"),
        ],
    ),
    "create/ignore: a non-literal create on a name outside the table": (
        _MOCK + 'mock.patch.object(onboarding_import, "_not_mirrored", 1, create=flag)\n'
        "mock.patch.multiple(onboarding_import, create=flag, _not_mirrored=1)\n",
        [],
    ),
    # 7. create=False, and no create at all, are the safe spellings.
    "create-false/flag: the same patches with create=True": (
        _MOCK + 'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        'mock.patch("kiro_crew.onboarding_import._toml", None, create=True)\n',
        [(3, "_MAX_FILES"), (4, "_toml")],
    ),
    "create-false/ignore": (
        _MOCK + 'mock.patch.object(onboarding_import, "_MAX_FILES", 1, create=False)\n'
        'mock.patch("kiro_crew.onboarding_import._toml", None, create=False)\n'
        "mock.patch.multiple(onboarding_import, create=False, _MAX_DB_ROWS=1)\n"
        'mock.patch.object(onboarding_import, "_MAX_FILES", 1)\n',
        [],
    ),
    # 8. On the facade, a name the source only computes at runtime is a hit.
    "dynamic/flag": (
        _MOCK + 'FACADE = "kiro_crew.onboarding_import"\n'
        "mock.patch.object(onboarding_import, name, 1, create=True)\n"
        'mock.patch(f"kiro_crew.onboarding_import.{name}", 1, create=True)\n'
        'mock.patch(FACADE + "." + name, 1, create=True)\n'
        "mock.patch.multiple(onboarding_import, create=True, **names)\n",
        [(4, _DYNAMIC), (5, _DYNAMIC), (6, _DYNAMIC), (7, _DYNAMIC)],
    ),
    "dynamic/ignore: off the facade, or with create=False": (
        _MOCK + "mock.patch.object(onboarding_scan, name, 1, create=True)\n"
        "mock.patch(target_name, 1, create=True)\n"
        "mock.patch.object(onboarding_import, name, 1, create=False)\n",
        [],
    ),
    # 9. Assigned aliases of the mock module, ``patch`` and its methods, in any order.
    "mock-alias/flag: assigned mock modules and patch callables": (
        "import importlib as il\n"
        "import sys\n"
        "import unittest.mock\n"
        "from unittest import mock\n"
        "from kiro_crew import onboarding_import\n"
        "m = unittest.mock\n"
        "m2 = m\n"
        "p = mock.patch\n"
        "patcher = mock.patch.object\n"
        "multi = p.multiple\n"
        'lm = il.import_module("unittest.mock")\n'
        'sm = sys.modules["unittest.mock"]\n'
        'm2.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        'p("kiro_crew.onboarding_import._toml", None, create=True)\n'
        'patcher(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        "multi(onboarding_import, create=True, _MAX_DB_ROWS=1)\n"
        'lm.patch.object(onboarding_import, "_toml", None, create=True)\n'
        'sm.patch.object(onboarding_import, "_toml", None, create=True)\n'
        'late(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        "late = patcher\n"
        "load = il.import_module\n"
        'load("unittest.mock").patch("kiro_crew.onboarding_import._toml", None, create=True)\n',
        [
            (13, "_MAX_FILES"),
            (14, "_toml"),
            (15, "_MAX_FILES"),
            (16, "_MAX_DB_ROWS"),
            (17, "_toml"),
            (18, "_toml"),
            (19, "_MAX_FILES"),
            (22, "_toml"),
        ],
    ),
    "mock-alias/ignore: assigned names that are not mock's patch": (
        "import importlib as il\n"
        "import requests\n"
        "from unittest import mock\n"
        "from kiro_crew import onboarding_import\n"
        "m = requests\n"
        "p = requests.patch\n"
        "patcher = mock.MagicMock\n"
        'lm = il.import_module("requests")\n'
        'm.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        'p("kiro_crew.onboarding_import._toml", None, create=True)\n'
        'patcher(onboarding_import, "_MAX_FILES", 1, create=True)\n'
        'lm.patch.object(onboarding_import, "_toml", None, create=True)\n',
        [],
    ),
    # 10. Facade aliases, resolved to a fixed point.
    "facade-alias/flag": (
        _MOCK + "import importlib\n"
        "import sys\n"
        "from importlib import import_module as load\n"
        "def _api():\n"
        "    return m\n"
        'mock.patch.object(m, "_toml", None, create=True)\n'
        "m = oi\n"
        "oi = onboarding_import\n"
        'loaded = importlib.import_module("kiro_crew.onboarding_import")\n'
        'mock.patch.object(loaded, "_toml", None, create=True)\n'
        'mock.patch.object(load(onboarding_import.__name__), "_toml", None, create=True)\n'
        'mock.patch.object(_api(), "_toml", None, create=True)\n'
        'mock.patch.object(sys.modules["kiro_crew.onboarding_import"], "_toml", create=True)\n',
        [(8, "_toml"), (12, "_toml"), (13, "_toml"), (14, "_toml"), (15, "_toml")],
    ),
    "facade-alias/ignore: aliases of the owner": (
        _MOCK + "import importlib\n"
        "oi = onboarding_scan\n"
        "m = oi\n"
        'mock.patch.object(m, "_MAX_FILES", 1, create=True)\n'
        'loaded = importlib.import_module("kiro_crew.onboarding_scan")\n'
        'mock.patch.object(loaded, "_MAX_FILES", 1, create=True)\n',
        [],
    ),
}


@pytest.mark.parametrize("case", sorted(_GUARD_CASES))
def test_the_create_detector_answers_both_ways(case: str) -> None:
    """The detector the guard below rests on, pinned on synthetic sources.

    A detector that matches nothing would make the guard pass while checking
    nothing, so every rule it enforces is measured on a source it must flag and a
    source it must ignore.
    """
    source, expected = _GUARD_CASES[case]
    hits = mirrored_create_patches(ast.parse(source), onboarding_import._EXPORTS)
    assert [(hit.line, hit.name) for hit in hits] == expected


def test_a_hit_is_attributed_to_the_test_that_holds_it() -> None:
    """The allowlist key is the enclosing test, a decorator's included."""
    source = (
        _MOCK + "class TestSeam:\n"
        '    @mock.patch.object(onboarding_import, "_toml", None, create=True)\n'
        "    def test_decorated(self):\n"
        "        pass\n"
        "    def test_inline(self):\n"
        '        mock.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
        'mock.patch("kiro_crew.onboarding_import._MAX_FILES", 1, create=True)\n'
    )
    hits = mirrored_create_patches(ast.parse(source), onboarding_import._EXPORTS)
    assert [hit.scope for hit in hits] == [
        "TestSeam.test_decorated",
        "TestSeam.test_inline",
        "<module>",
    ]


def test_the_allowlist_must_equal_the_scan() -> None:
    """An unlisted hit and a stale entry both fail; an exact match is the only pass."""
    hit = ("test/test_example.py", "TestSeam.test_premise")
    # Must flag: a hit the allowlist does not name, and an entry nothing hits.
    assert _allowlist_mismatch({hit}, frozenset()) == ({hit}, set())
    assert _allowlist_mismatch(set(), frozenset({hit})) == (set(), {hit})
    # Must ignore: a hit the allowlist names exactly.
    assert _allowlist_mismatch({hit}, frozenset({hit})) == (set(), set())


def _patching_test_sources() -> list[tuple[Path, str]]:
    """Every test file whose text contains ``onboarding_import``, from the files git sees.

    ``test/`` and every ``tests/`` package under ``src/``. Read through
    ``source_corpus.repo_files`` rather than a walk, so another checkout under the
    repository is never mistaken for this one. The text filter keeps the scan off
    the thousands of files that never mention the facade (parsing all of them takes
    about a minute). Every import, alias, ``__name__`` and dotted-string spelling
    the guard resolves contains that text, so the one spelling it skips is the
    module name split across separate string literals.
    """
    root = source_corpus.repo_root()
    rows: list[tuple[Path, str]] = []
    for path in source_corpus.repo_files():
        if path.suffix != ".py":
            continue
        parts = path.relative_to(root).parts
        if not (parts[0] == "test" or (parts[0] == "src" and "tests" in parts[:-1])):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "onboarding_import" in text:
            rows.append((path, text))
    return rows


def test_no_test_patches_a_mirrored_name_with_create() -> None:
    """The raw scan of every test file equals the allowlist, which is empty."""
    root = source_corpus.repo_root()
    sources = _patching_test_sources()
    assert Path(__file__).resolve() in {path.resolve() for path, _text in sources}, (
        "the scan did not reach this file, which names the facade, so it reads "
        "nothing and its empty result means nothing"
    )
    found: dict[tuple[str, str], list[_Hit]] = {}
    for path, text in sources:
        relative = path.relative_to(root).as_posix()
        for hit in mirrored_create_patches(ast.parse(text), onboarding_import._EXPORTS):
            found.setdefault((relative, hit.scope), []).append(hit)
    unexpected, stale = _allowlist_mismatch(set(found), _CREATE_PATCH_ALLOWLIST)
    assert set(found) == _CREATE_PATCH_ALLOWLIST, (
        "mock.patch with a create other than the literal False on a mirrored "
        "onboarding_import name deletes the owner's binding on exit and never puts it "
        f"back. Unlisted: {sorted((key, found[key]) for key in unexpected)}. Allowlist "
        f"entries nothing hits: {sorted(stale)}. Drop the create; the name exists."
    )


# ── A facade patch reaches every reader ────────────────────────────────────────
#
# Besides the mirrored seams, the facade binds by-name copies: the apply owner's
# strategy constants, the registry's helpers, the scan owner's screens and the
# public names the module bound before the split. The facade's own code reads a
# few of them and no owner reads the facade's copy, so a test that rebinds one on
# the facade changes that copy while every owner call site keeps its own binding.
# The guard below reads what the tests rebind on the facade off the tests
# themselves and refuses a name that an engine module other than the facade also
# binds, unless that module is the name's owner in ``_EXPORTS``.


def _engine_bound_names() -> set[str]:
    """Every name an engine module binds, plus the mirrored seams."""
    return set(onboarding_import._EXPORTS).union(
        *(vars(importlib.import_module(module)) for module in _ENGINE_MODULES)
    )


def facade_rebinds(tree: ast.Module, names: Collection[str]) -> set[str]:
    """The names in *names* that *tree* rebinds or deletes on the facade.

    A rebind is a ``patch``, ``patch.object`` or ``patch.multiple`` call or
    decorator the create guard resolves to the facade, or a ``setattr`` /
    ``delattr`` on any receiver (``monkeypatch``, the builtins) whose target is the
    facade with a static attribute name or a dotted ``kiro_crew.onboarding_import``
    string. ``patch.dict`` is not one: it mutates the shared object in place, so
    every holder sees it. A name the source only computes at runtime is skipped.
    """
    bindings = _bindings(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = _patch_method(node.func, bindings)
        if method is not None:
            if method != "dict":
                found.update(_patched_names(node, method, bindings, names))
            continue
        func = node.func
        verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if verb not in ("setattr", "delattr") or not node.args:
            continue
        if _is_facade(node.args[0], bindings):
            attribute = _static_text(node.args[1], bindings) if len(node.args) > 1 else _HOLE
            found.update([attribute] if attribute in names else [])
        else:
            found.update(_addressed(_static_text(node.args[0], bindings), names))
    return found - {_DYNAMIC}


def _other_holders(name: str) -> list[str]:
    """Engine modules other than the facade and *name*'s owner that bind *name*."""
    return [
        module
        for module in _ENGINE_MODULES
        if module not in (_FACADE, onboarding_import._EXPORTS.get(name))
        and name in vars(importlib.import_module(module))
    ]


_REBIND_PROBE = (
    "import importlib\n"
    "from unittest import mock\n"
    "import kiro_crew.onboarding_import as backend\n"
    "from kiro_crew import onboarding_import, onboarding_sources\n"
    "def _api():\n"
    '    return importlib.import_module("kiro_crew.onboarding_import")\n'
    "def test_probe(monkeypatch, name):\n"
    "    api = _api()\n"
    # Must flag: each spelling of a rebind on the facade.
    '    monkeypatch.setattr(api, "_managed_mcp_names", frozenset)\n'
    '    monkeypatch.setattr(backend, "CATEGORY_IDS", ())\n'
    '    monkeypatch.setattr("kiro_crew.onboarding_import.STRATEGY_SKIP", "rename")\n'
    '    monkeypatch.delattr(onboarding_import, "_SOURCE_ID_RE")\n'
    '    setattr(onboarding_import, "split_frontmatter", None)\n'
    '    mock.patch.object(onboarding_import, "_sources", dict)\n'
    '    mock.patch("kiro_crew.onboarding_import.CONFLICT_STRATEGIES", ())\n'
    '    mock.patch.multiple(onboarding_import, STRATEGY_RENAME="r")\n'
    # Must ignore: the owner, an attribute of a module the facade shares, a name
    # computed at runtime, an in-place dict patch and the owner's dotted name.
    '    monkeypatch.setattr(onboarding_sources, "_managed_mcp_names", frozenset)\n'
    '    monkeypatch.setattr(onboarding_import.platform_compat, "IS_WINDOWS", True)\n'
    "    monkeypatch.setattr(onboarding_import, name, None)\n"
    "    mock.patch.dict(onboarding_import._EXPORTS, {})\n"
    '    mock.patch("kiro_crew.onboarding_scan.CATEGORY_IDS", ())\n'
)


def test_the_rebind_guard_answers_both_ways() -> None:
    """The scan and the holder check the guard rests on, pinned on a planted source."""
    planted = facade_rebinds(ast.parse(_REBIND_PROBE), _engine_bound_names())
    assert planted == {
        "CATEGORY_IDS",
        "CONFLICT_STRATEGIES",
        "STRATEGY_RENAME",
        "STRATEGY_SKIP",
        "_SOURCE_ID_RE",
        "_managed_mcp_names",
        "_sources",
        "split_frontmatter",
    }
    # Each planted name is a copy another engine module also binds, so a real
    # test that rebinds any of them on the facade fails the guard below.
    assert {name: bool(_other_holders(name)) for name in planted} == dict.fromkeys(planted, True)
    # A mirrored seam (its owner holds the one binding) and a name only the facade
    # binds have no other holder.
    assert _other_holders("_write_json") == []
    assert _other_holders("make_sync_embed_fn") == []


def test_no_engine_module_binds_a_name_tests_rebind_on_the_facade() -> None:
    """A name a test rebinds on the facade has one binding, so the patch applies.

    That binding is the owner's for a mirrored seam and the facade's for a name
    only the facade binds. Any other engine module holding the name keeps reading
    its own copy, so the patch silently stops applying there: mirror the name in
    ``_EXPORTS``, whose owner then holds the one binding, or patch the owner.
    """
    names = _engine_bound_names()
    patched = set().union(
        *(facade_rebinds(ast.parse(text), names) for _path, text in _patching_test_sources())
    )
    # Non-vacuous: the scan sees what the engine's own suites rebind on the facade,
    # mirrored seams and facade-only names both.
    expected = {"_is_link_like", "_write_json", "_MAX_LESSONS_TOTAL", "make_sync_embed_fn"}
    assert expected <= patched, sorted(expected - patched)
    stray = {name: holders for name in sorted(patched) if (holders := _other_holders(name))}
    assert stray == {}, (
        "these names are rebound on onboarding_import by a test but are also bound "
        f"by another engine module, which keeps reading its own copy: {stray}. "
        "Mirror the name in _EXPORTS, or patch the module that reads it."
    )


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guards read every test module, so a leaf-only diff must still run them."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    # The join with the test directory on the next line is load-bearing: it is the only
    # text in this file that corpus_gates() reads as reaching the test tree, so spelling
    # this path any other way drops the file from the gates and fails this test.
    this_file = (root / "test" / Path(__file__).name).relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)
