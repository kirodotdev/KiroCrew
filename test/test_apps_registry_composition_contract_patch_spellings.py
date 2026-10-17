"""The App Registry's composition: no test patches a forwarded name with ``create=True``.

``mock.patch`` undoes a name ``kiro_crew.apps.registry`` forwards to its owner by
deleting it, and under ``create=True`` that delete is the whole undo: the name is
gone from every registry module for the rest of the run. The guard here reads every
test module of the repository off its AST alone and finds each patch of a forwarded
name whose ``create`` is not literally ``False``, through any import alias, call or
decorator spelling; the reader is shown each form it must catch beside a spelling
of it it must leave alone. The rest of the facade's contract is in
``test_apps_registry_composition_contract.py``, which holds the one allowlisted
premise case.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
import source_corpus

from kiro_crew.apps import registry

# One group per file: the patch-spelling guard parses every test module, and the
# production re-export scan parses part of the package, once per worker otherwise.
pytestmark = pytest.mark.xdist_group(name="tree_scan_apps_registry_composition_patch_spellings")

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: An exported name several owners import: defined in ``git_targets``.
_MULTI_HOLDER = "_strip_git_target_userinfo"


# ---------------------------------------------------------------------------
# The patch spellings the suite may use on the facade
# ---------------------------------------------------------------------------

#: The patch callables by the dotted path they resolve to: which one, and the index of
#: ``create`` among its positional parameters.
_PATCH_CALLABLES = {
    "unittest.mock.patch": ("patch", 3),
    "unittest.mock.patch.object": ("object", 4),
    "unittest.mock.patch.multiple": ("multiple", 2),
}

#: ``patch.multiple`` keywords that configure the patch rather than name an attribute.
_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})

#: What a hit names when the patched attribute cannot be read off the source.
_DYNAMIC = "<dynamic>"

#: The one deliberate ``create=True`` patch of a forwarded name, keyed by file and the
#: test enclosing it: the premise case, which shows that such a patch still deletes the
#: name and puts every holder back itself.
_ALLOWED_CREATE_TRUE = frozenset(
    {
        (
            "test/test_apps_registry_composition_contract.py",
            "TestOneNamespaceForWrites."
            "test_create_true_on_a_forwarded_name_deletes_it_from_every_holder",
        )
    }
)

#: A value an expression may denote: a dotted ``path`` (a module, or an attribute reached
#: from one), a ``str``, or the known leading ``prefix`` of a string.
_Value = tuple[str, str]


class _Hit(NamedTuple):
    function: str
    name: str
    line: int


class _Resolver:
    """What the names in one module's source may denote, read off its AST alone.

    A name is bound by an import, or by a plain assignment in its function's scope or an
    enclosing one, followed to a fixed point; a name bound more than once may denote any
    of its values. An expression the reader cannot follow denotes nothing.
    """

    def __init__(
        self,
        tree: ast.Module,
        module: str | None,
        reexports: frozenset[str],
        *,
        is_package: bool = False,
    ) -> None:
        self._tree = tree
        # A package's ``__init__`` resolves ``from . import x`` against itself.
        self._package = (module if is_package else module.rpartition(".")[0]) if module else None
        self._reexports = reexports
        self._parents: dict[ast.AST, ast.AST] = {}
        self._bindings: dict[ast.AST, dict[str, list[ast.AST | frozenset[_Value]]]] = {}
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
                stack.append(child)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, path in self._imported(node):
                    self._bind(node, name, frozenset({("path", path)}))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._bind(node, target.id, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self._bind(node, node.target.id, node.value)
        self._following: set[tuple[int, str]] = set()
        self._known: dict[tuple[int, str], set[_Value]] = {}
        # Set while resolving a name whose chain looped back on itself: a result
        # computed then is partial, so it is returned but not remembered.
        self._cut = False

    def _imported(self, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (alias.asname, alias.name) if alias.asname else (alias.name.split(".")[0],) * 2
                for alias in node.names
            ]
        module = node.module or ""
        if node.level:
            if self._package is None:
                return []
            module = importlib.util.resolve_name("." * node.level + module, self._package)
        return [(alias.asname or alias.name, f"{module}.{alias.name}") for alias in node.names]

    def _bind(self, statement: ast.AST, name: str, value: ast.AST | frozenset[_Value]) -> None:
        self._bindings.setdefault(self.scope_of(statement), {}).setdefault(name, []).append(value)

    def scope_of(self, node: ast.AST) -> ast.AST:
        """The function whose body holds *node*, or the module; a decorator is outside."""
        child, parent = node, self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                decorators = getattr(parent, "decorator_list", [])
                if not any(child is decorator for decorator in decorators):
                    return parent
            child, parent = parent, self._parents.get(parent)
        return self._tree

    def function_of(self, node: ast.AST) -> str:
        """The dotted class and function names enclosing *node*, ``<module>`` for none."""
        names = []
        parent = self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(parent.name)
            parent = self._parents.get(parent)
        return ".".join(reversed(names)) or "<module>"

    def module_names(self) -> list[str]:
        return list(self._bindings.get(self._tree, {}))

    def is_facade(self, value: _Value) -> bool:
        kind, text = value
        return kind == "path" and (text == registry.__name__ or text in self._reexports)

    def values(self, expr: ast.AST | None, scope: ast.AST) -> set[_Value]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            return {("str", expr.value)} if isinstance(expr.value, str) else set()
        if isinstance(expr, ast.Name):
            return self._name(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            found: set[_Value] = set()
            for value in self.values(expr.value, scope):
                if value[0] == "path":
                    found.add(("path", f"{value[1]}.{expr.attr}"))
                    if expr.attr == "__name__" and self.is_facade(value):
                        found.add(("str", registry.__name__))
            return found
        if isinstance(expr, ast.Call) and len(expr.args) == 1:
            if ("path", "importlib.import_module") in self.values(expr.func, scope):
                return {
                    ("path", text)
                    for kind, text in self.values(expr.args[0], scope)
                    if kind == "str"
                }
            return set()
        if isinstance(expr, ast.JoinedStr):
            return self._joined(expr.values, scope)
        if isinstance(expr, ast.FormattedValue):
            plain = expr.conversion == -1 and expr.format_spec is None
            return self.values(expr.value, scope) if plain else set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self._joined([expr.left, expr.right], scope)
        return set()

    def _joined(self, parts: list[ast.expr], scope: ast.AST) -> set[_Value]:
        text = ""
        for part in parts:
            values = self.values(part, scope)
            strings = {value for kind, value in values if kind == "str"}
            prefixes = {value for kind, value in values if kind == "prefix"}
            if len(strings) == 1 and not prefixes:
                text += strings.pop()
                continue
            if len(prefixes) == 1 and not strings:
                text += prefixes.pop()
            return {("prefix", text)} if text else set()
        return {("str", text)}

    def _name(self, name: str, scope: ast.AST) -> set[_Value]:
        key = (id(scope), name)
        if key in self._known:
            return self._known[key]
        if key in self._following:
            self._cut = True
            return set()
        self._following.add(key)
        outer_cut, self._cut = self._cut, False
        try:
            found: set[_Value] = set()
            for binding_scope in self._chain(scope):
                bound = self._bindings.get(binding_scope, {}).get(name)
                if bound is not None:
                    for value in bound:
                        found |= (
                            value
                            if isinstance(value, frozenset)
                            else self.values(value, binding_scope)
                        )
                    break
            if not self._cut:
                self._known[key] = found
            return found
        finally:
            self._following.discard(key)
            self._cut = outer_cut or self._cut

    def _chain(self, scope: ast.AST) -> Iterator[ast.AST]:
        while scope is not self._tree:
            yield scope
            scope = self.scope_of(scope)
        yield self._tree

    def hits(self, call: ast.Call) -> list[str]:
        """The forwarded names *call* patches with ``create`` not literally ``False``."""
        scope = self.scope_of(call)
        callable_ = next(
            (
                _PATCH_CALLABLES[text]
                for kind, text in self.values(call.func, scope)
                if text in _PATCH_CALLABLES
            ),
            None,
        )
        if callable_ is None:
            return []
        kind, create_at = callable_
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        splat = any(keyword.arg is None for keyword in call.keywords)
        create = keywords.get(
            "create", call.args[create_at] if len(call.args) > create_at else None
        )
        if create is None and splat:
            # A ``**`` splat may carry ``create``: unknown is not literally False.
            create = ast.Name(id="<splat>")
        if create is None or (isinstance(create, ast.Constant) and create.value is False):
            return []
        target = keywords.get("target", call.args[0] if call.args else None)
        targets = self.values(target, scope)
        prefix = registry.__name__ + "."
        found: set[str] = set()
        if kind == "patch":
            for value_kind, text in targets:
                rest = text[len(prefix) :] if text.startswith(prefix) else None
                if rest is None or "." in rest:
                    continue
                found.add(rest if value_kind == "str" else _DYNAMIC)
        elif kind == "object":
            if any(self.is_facade(value) for value in targets):
                attribute = keywords.get("attribute", call.args[1] if len(call.args) > 1 else None)
                names = {
                    text
                    for value_kind, text in self.values(attribute, scope)
                    if value_kind == "str"
                }
                found = names if names else {_DYNAMIC}
        elif (
            any(self.is_facade(value) for value in targets) or ("str", registry.__name__) in targets
        ):
            found = {name for name in keywords if name not in _MULTIPLE_OPTIONS}
            if any(keyword.arg is None for keyword in call.keywords):
                found.add(_DYNAMIC)
        return sorted(name for name in found if name == _DYNAMIC or name in registry._EXPORTS)


def _module_name(path: Path) -> str | None:
    """The dotted import name of a file under ``src/``; a file elsewhere has none."""
    relative = path.relative_to(_REPO_ROOT)
    if relative.parts[0] != "src":
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_package(path: Path) -> bool:
    return path.name == "__init__.py"


def _in_a_test_directory(path: Path) -> bool:
    return any(part.endswith("tests") for part in path.relative_to(_REPO_ROOT).parts[:-1])


def _python_files() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every ``.py`` file the checkout holds that names the registry.

    Enumerated the way git sees the checkout (``source_corpus.repo_files``), so a
    nested worktree or scratch copy never stands in for the file it copies.
    """
    for path in source_corpus.repo_files_named(".py"):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not relative.startswith(("test/", "src/")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "registry" in text:
            yield path, text


@functools.lru_cache(maxsize=1)
def _facade_reexports() -> frozenset[str]:
    """Dotted paths that name the facade through a production module importing it.

    Read off the source the same way the guard reads a test, to a fixed point, so a
    module re-exporting another's alias is covered, and a new re-export needs no edit
    here.
    """
    candidates: list[tuple[str, bool, ast.Module]] = []
    for path, text in _python_files():
        if _module_name(path) is None or _in_a_test_directory(path):
            continue
        if "kiro_crew.apps" in text or "apps" in path.relative_to(_REPO_ROOT).parts:
            candidates.append((_module_name(path) or "", _is_package(path), ast.parse(text)))
    found: frozenset[str] = frozenset()
    while True:
        grown = set(found)
        for module, is_package, tree in candidates:
            resolver = _Resolver(tree, module, found, is_package=is_package)
            for name in resolver.module_names():
                if any(resolver.is_facade(v) for v in resolver.values(ast.Name(id=name), tree)):
                    grown.add(f"{module}.{name}")
        if grown == found:
            return found
        found = frozenset(grown)


def _may_pass_create(call: ast.Call) -> bool:
    """Whether *call* could hand a patch a ``create`` that is not literally ``False``.

    A ``create`` keyword that is not the literal ``False``, a ``**`` splat that could
    carry one, or three positional arguments or more -- where ``create`` could sit
    positionally under any alias of a patch callable. Only these calls are worth
    resolving; which callable a call names is the resolver's question.
    """
    for keyword in call.keywords:
        if keyword.arg == "create":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
    return any(keyword.arg is None for keyword in call.keywords) or len(call.args) >= 3


def _create_true_patches_of_forwarded_names(
    source: str,
    module: str | None = None,
    reexports: frozenset[str] | None = None,
    *,
    is_package: bool = False,
) -> list[_Hit]:
    """Every ``mock.patch`` of a forwarded name in *source* whose ``create`` is not
    literally ``False``.

    Any spelling the reader can resolve counts: ``patch``, ``patch.object`` and
    ``patch.multiple`` reached through any import alias, called or used as a
    decorator, with a positional or keyword target and attribute, a keyword
    ``create`` -- or a positional one to a callable spelled ``patch``, ``object`` or
    ``multiple`` -- and a string target built from the facade's name. A patch of the
    facade whose attribute it cannot resolve is a ``<dynamic>`` hit, never a pass. A
    name the facade binds itself is safe -- ``mock.patch`` sees it as local and
    writes the original back.
    """
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _may_pass_create(node)
    ]
    if not calls:
        return []
    resolver = _Resolver(
        tree,
        module,
        _facade_reexports() if reexports is None else reexports,
        is_package=is_package,
    )
    return [
        _Hit(resolver.function_of(call), name, call.lineno)
        for call in calls
        for name in resolver.hits(call)
    ]


def _patch_sources() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every test module of the repository that may patch the facade.

    ``test/`` and every directory under ``src/`` whose name ends in ``tests``, handed
    to the AST reader when the text names ``registry`` and a patch helper. The word
    ``create`` is not required: it can arrive positionally or through a ``**`` splat.
    """
    for path, text in _python_files():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not (relative.startswith("test/") or _in_a_test_directory(path)):
            continue
        if "patch" in text or "mock" in text:
            yield path, text


#: Imports most reader cases share.
_CASE_IMPORTS = "from unittest import mock\nfrom kiro_crew.apps import registry\n"

#: ``(id, source, module, expected names)`` for the reader: each form the guard must
#: catch beside a spelling of it the guard must leave alone. ``@EXP@`` is a forwarded
#: name, ``@BOUND@`` one the facade binds itself, ``@FACADE@`` the facade's dotted name.
_READER_CASES: list[tuple[str, str, str | None, list[str]]] = [
    (
        "patch.object",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "create=False",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', create=False)\n",
        None,
        [],
    ),
    ("no create", _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@')\n", None, []),
    (
        "create positionally",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', mock.DEFAULT, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "False positionally",
        _CASE_IMPORTS + "mock.patch.object(registry, '@EXP@', mock.DEFAULT, None, False)\n",
        None,
        [],
    ),
    (
        "create not a literal",
        _CASE_IMPORTS + "flag = True\nmock.patch.object(registry, '@EXP@', create=flag)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a name the facade binds",
        _CASE_IMPORTS + "mock.patch.object(registry, '@BOUND@', create=True)\n",
        None,
        [],
    ),
    (
        "a name no module holds",
        _CASE_IMPORTS + "mock.patch.object(registry, '_no_such_registry_helper', create=True)\n",
        None,
        [],
    ),
    (
        "another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(config, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "another module's .registry",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "mock.patch.object(config.registry, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "the facade module imported by its path",
        "from unittest import mock\nimport kiro_crew.apps.registry as reg\n"
        "mock.patch.object(reg, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "the facade module reached through its package",
        "from unittest import mock\nimport kiro_crew.apps\n"
        "mock.patch.object(kiro_crew.apps.registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch as an alias",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps import registry\n"
        "P.object(registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a local function named patch",
        "from kiro_crew.apps import registry\ndef patch(*a, **k):\n    return None\n"
        "patch.object(registry, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "mock as an alias",
        "from unittest import mock as M\nfrom kiro_crew.apps import registry\n"
        "M.patch.object(registry, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock as an alias",
        "import unittest.mock as um\num.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock by its path",
        "import unittest.mock\nfrom kiro_crew.apps import registry\n"
        "unittest.mock.patch.multiple(registry, create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a third-party mock",
        "import mock\nfrom kiro_crew.apps import registry\n"
        "mock.patch.object(registry, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "as a decorator",
        _CASE_IMPORTS
        + "@mock.patch.object(registry, '@EXP@', create=True)\ndef test_x(fake):\n    pass\n",
        None,
        ["@EXP@"],
    ),
    (
        "a decorator with create=False",
        _CASE_IMPORTS
        + "@mock.patch.object(registry, '@EXP@', create=False)\ndef test_x(fake):\n    pass\n",
        None,
        [],
    ),
    (
        "a dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a deeper dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.os.utime', create=True)\n",
        None,
        [],
    ),
    (
        "another dotted string",
        "from unittest import mock\nmock.patch('kiro_crew.config.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of __name__",
        _CASE_IMPORTS + "mock.patch(f'{registry.__name__}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another __name__",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch(f'{config.__name__}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of a constant",
        "from unittest import mock\nFACADE = '@FACADE@'\n"
        "mock.patch(f'{FACADE}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another constant",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\n"
        "mock.patch(f'{OTHER}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a constant concatenated",
        "from unittest import mock\nFACADE = '@FACADE@'\n"
        "mock.patch(FACADE + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "__name__ concatenated",
        _CASE_IMPORTS + "mock.patch(registry.__name__ + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "another constant concatenated",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\n"
        "mock.patch(OTHER + '.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword target and attribute",
        _CASE_IMPORTS + "mock.patch.object(target=registry, attribute='@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "keyword target elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "mock.patch.object(target=config, attribute='@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword string target",
        "from unittest import mock\nmock.patch(target='@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an unresolved attribute",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(registry, attr, create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an unresolved attribute, create=False",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(registry, attr, create=False)\n",
        None,
        [],
    ),
    (
        "a resolved local attribute",
        _CASE_IMPORTS
        + "def test_x():\n    name = '@EXP@'\n    mock.patch.object(registry, name, create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string it cannot finish",
        _CASE_IMPORTS
        + "def test_x(attr):\n    mock.patch(f'{registry.__name__}.{attr}', create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an aliased patch with a positional create",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps import registry\n"
        "P.object(registry, '@EXP@', None, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an aliased dotted-string patch with a positional create",
        "from unittest.mock import patch as P\nP('@FACADE@.@EXP@', None, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a splat that may carry create",
        _CASE_IMPORTS + "def test_x(opts):\n    mock.patch.object(registry, '@EXP@', **opts)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a splat on another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "def test_x(opts):\n    mock.patch.object(config, '@EXP@', **opts)\n",
        None,
        [],
    ),
    (
        "an alias whose chain loops back on itself",
        _CASE_IMPORTS
        + "a = registry\na = b\nb = a\n"
        + "mock.patch.object(a, '@EXP@', create=True)\n"
        + "mock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@", "@EXP@"],
    ),
    (
        "**kwargs in patch.multiple",
        _CASE_IMPORTS + "def test_x(kw):\n    mock.patch.multiple(registry, create=True, **kw)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\n"
        + "def test_x(kw):\n    mock.patch.multiple(config, create=True, **kw)\n",
        None,
        [],
    ),
    (
        "patch.multiple of a dotted string",
        "from unittest import mock\nmock.patch.multiple('@FACADE@', create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch.multiple of another string",
        "from unittest import mock\n"
        "mock.patch.multiple('kiro_crew.config', create=True, @EXP@=None)\n",
        None,
        [],
    ),
    (
        "import_module",
        "import importlib\nfrom unittest import mock\n"
        "facade = importlib.import_module('@FACADE@')\n"
        "mock.patch.object(facade, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module elsewhere",
        "import importlib\nfrom unittest import mock\n"
        "other = importlib.import_module('kiro_crew.config')\n"
        "mock.patch.object(other, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a chain of assignments",
        _CASE_IMPORTS + "a = registry\nb = a\nmock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an assignment inside the test",
        _CASE_IMPORTS
        + "def test_x():\n    reg = registry\n    mock.patch.object(reg, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a relative import in a package",
        "from unittest import mock\nfrom ... import registry\n"
        "mock.patch.object(registry, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.tests.test_case",
        ["@EXP@"],
    ),
    (
        "a relative import elsewhere",
        "from unittest import mock\nfrom ..crew import registry\n"
        "mock.patch.object(registry, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.tests.test_case",
        [],
    ),
]

#: A production module that re-exports the facade under another name, for the two
#: cases below: the scan finds none today, so the reader is shown one.
_SYNTHETIC_REEXPORTS = frozenset({"kiro_crew.apps.routes.registry_mod"})

_REEXPORT_CASES: list[tuple[str, str, list[str]]] = [
    (
        "an assigned re-export",
        "from unittest import mock\nfrom kiro_crew.apps import routes\n"
        "def test_x():\n    reg = routes.registry_mod\n"
        "    mock.patch.object(reg, '@EXP@', create=True)\n",
        ["@EXP@"],
    ),
    (
        "an assigned other attribute",
        "from unittest import mock\nfrom kiro_crew.apps import routes\n"
        "def test_x():\n    reg = routes.registry_other\n"
        "    mock.patch.object(reg, '@EXP@', create=True)\n",
        [],
    ),
]


def _case(template: str) -> str:
    return (
        template.replace("@EXP@", _MULTI_HOLDER)
        .replace("@BOUND@", "_run_app_build")
        .replace("@FACADE@", registry.__name__)
    )


class TestPatchSpellings:
    def test_the_case_names_are_what_they_claim(self) -> None:
        assert _MULTI_HOLDER in registry._EXPORTS
        assert "_run_app_build" in vars(registry) and "_run_app_build" not in registry._EXPORTS
        assert "_no_such_registry_helper" not in registry._EXPORTS
        assert not hasattr(registry, "_no_such_registry_helper")

    def test_every_production_alias_the_scan_finds_is_the_facade(self) -> None:
        # Production code reaches the facade by name or by a function-local import
        # today (``platform/defaults.py``), so the scan finds no module-level alias; a
        # new one extends the guard's reach with no edit here, and whatever it finds
        # must really be the facade at run time.
        for dotted in _facade_reexports():
            module, _, name = dotted.rpartition(".")
            assert getattr(importlib.import_module(module), name) is registry, dotted

    @pytest.mark.parametrize(
        ("is_package", "expected"),
        [(True, [_MULTI_HOLDER]), (False, [])],
        ids=["a package __init__", "a plain module of the same name"],
    )
    def test_a_relative_import_resolves_against_the_right_package(
        self, is_package: bool, expected: list[str]
    ) -> None:
        # ``from ... import registry`` in ``kiro_crew/apps/builtins/tests/__init__.py``
        # names ``kiro_crew.apps.registry``; in a plain ``kiro_crew/apps/builtins/tests.py``
        # it names ``kiro_crew.registry``, which is not the facade.
        source = _case(
            "from unittest import mock\nfrom ... import registry\n"
            "mock.patch.object(registry, '@EXP@', create=True)\n"
        )
        hits = _create_true_patches_of_forwarded_names(
            source, "kiro_crew.apps.builtins.tests", is_package=is_package
        )
        assert [hit.name for hit in hits] == expected

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _READER_CASES],
        ids=[case[0] for case in _READER_CASES],
    )
    def test_the_reader_flags_every_spelling_and_only_those(
        self, source: str, module: str | None, expected: list[str]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that flagged
        # a safe spelling would stop a patch that undoes cleanly.
        hits = _create_true_patches_of_forwarded_names(_case(source), module)
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    @pytest.mark.parametrize(
        ("source", "expected"),
        [(case[1], case[2]) for case in _REEXPORT_CASES],
        ids=[case[0] for case in _REEXPORT_CASES],
    )
    def test_the_reader_follows_a_production_re_export(
        self, source: str, expected: list[str]
    ) -> None:
        hits = _create_true_patches_of_forwarded_names(
            _case(source), None, reexports=_SYNTHETIC_REEXPORTS
        )
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    def test_no_test_patches_a_forwarded_name_with_create_true(self) -> None:
        # ``mock.patch`` undoes a name the facade forwards by deleting it, and under
        # ``create=True`` the delete is the whole undo: the name is gone from every
        # registry module for the rest of the run, and a later test fails far from
        # here. The scan must find exactly the allowlisted premise case, so the
        # allowlist can neither hide a second site nor outlive the one it names.
        hits = [
            (path.relative_to(_REPO_ROOT).as_posix(), hit)
            for path, text in _patch_sources()
            for hit in _create_true_patches_of_forwarded_names(
                text, _module_name(path), is_package=_is_package(path)
            )
        ]
        found = {(path, hit.function) for path, hit in hits}
        # One hit exactly: the premise case holds a single patch, so a second site
        # added inside the same function is not hidden by the allowlist.
        assert len(hits) == len(_ALLOWED_CREATE_TRUE), hits
        unexpected = [
            f"{path}:{hit.line} {hit.function} patches {hit.name}"
            for path, hit in hits
            if (path, hit.function) not in _ALLOWED_CREATE_TRUE
        ]
        assert found == _ALLOWED_CREATE_TRUE, (
            "mock.patch(..., create=True) of a name kiro_crew.apps.registry forwards to "
            "its owner deletes that name from every registry module when the patch "
            "exits. Drop create=True (the name exists) or patch it with "
            f"monkeypatch.setattr: {unexpected}; allowlisted but not found: "
            f"{sorted(_ALLOWED_CREATE_TRUE - found)}"
        )


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard reads every test module, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    # The join with the test directory on the next line is load-bearing: it is the only
    # text in this file that corpus_gates() reads as reaching the test tree, so spelling
    # this path any other way drops the file from the gates and fails this test.
    this_file = (root / "test" / Path(__file__).name).relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)
