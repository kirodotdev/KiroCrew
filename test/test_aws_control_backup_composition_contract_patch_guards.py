"""Repository-wide guards on how a test patches the AWS Control backup facade and its parts.

Two guards scan the repository's test modules -- ``test/`` and each directory under
``src/`` whose name ends in ``tests`` -- parsing each one whose text could hold a hit and
reading it off its AST alone, following import aliases, assignments, dotted strings and a
production module's re-export of the facade to what they denote. ``TestPatchSpellings``
fails on a ``mock.patch``, ``patch.object`` or ``patch.multiple`` of a name
``backend.backup`` forwards to its owner that passes a ``create`` other than the literal
``False``, because that patch's exit deletes the name from every engine module.
``TestNoTestPatchesAPart`` fails on a write into the namespace of the ``backup_parts``
package or of a module in it, which reaches no other module holding the name. Each guard
pins the names or namespaces its cases rely on, shows its reader every form it must catch
beside a spelling of it the reader must leave alone, and requires its scan to find exactly
its allowlisted premise cases. Those premise cases live in
``test_aws_control_backup_composition_contract.py``, which pins the surface, the one
namespace for writes and the layering; ``PARTS``, ``_REPO_ROOT`` and ``_PARTS_PACKAGE``,
which both files read, live in ``aws_control_backup_composition_contract_helpers.py``.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from aws_control_backup_composition_contract_helpers import (
    _PARTS_PACKAGE,
    _REPO_ROOT,
    PARTS,
)

from kiro_crew.apps.builtins.aws_control.backend import backup
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import retention

# ---------------------------------------------------------------------------
# The patch spellings the suite may use on the facade
# ---------------------------------------------------------------------------

#: Directory names a repository-wide scan never enters.
_NOT_SCANNED = frozenset({".worktrees", "node_modules", ".venv", "__pycache__"})

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
            "test/test_aws_control_backup_composition_contract.py",
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

    def __init__(self, tree: ast.Module, module: str | None, reexports: frozenset[str]) -> None:
        self._tree = tree
        self._package = module.rpartition(".")[0] if module else None
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
        return kind == "path" and (text == backup.__name__ or text in self._reexports)

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
                        found.add(("str", backup.__name__))
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
            return set()
        self._following.add(key)
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
            self._known[key] = found
            return found
        finally:
            self._following.discard(key)

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
        create = keywords.get(
            "create", call.args[create_at] if len(call.args) > create_at else None
        )
        if create is None or (isinstance(create, ast.Constant) and create.value is False):
            return []
        target = keywords.get("target", call.args[0] if call.args else None)
        targets = self.values(target, scope)
        prefix = backup.__name__ + "."
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
        elif any(self.is_facade(value) for value in targets) or ("str", backup.__name__) in targets:
            found = {name for name in keywords if name not in _MULTIPLE_OPTIONS}
            if any(keyword.arg is None for keyword in call.keywords):
                found.add(_DYNAMIC)
        return sorted(name for name in found if name == _DYNAMIC or name in backup._EXPORTS)


def _module_name(path: Path) -> str | None:
    """The dotted import name of a file under ``src/``; a file elsewhere has none."""
    relative = path.relative_to(_REPO_ROOT)
    if relative.parts[0] != "src":
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _in_a_test_directory(path: Path) -> bool:
    return any(part.endswith("tests") for part in path.relative_to(_REPO_ROOT).parts[:-1])


@functools.lru_cache(maxsize=1)
def _facade_reexports() -> frozenset[str]:
    """Dotted paths that name the facade through a production module importing it.

    Read off the source the same way the guard reads a test, to a fixed point, so a module
    re-exporting another's alias is covered, and a new re-export needs no edit here.
    """
    candidates: list[tuple[str, ast.Module]] = []
    for path in (_REPO_ROOT / "src").rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if _NOT_SCANNED.intersection(relative.parts) or _in_a_test_directory(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "backup" in text and ("aws_control" in text or "aws_control" in relative.parts):
            candidates.append((_module_name(path) or "", ast.parse(text)))
    found: frozenset[str] = frozenset()
    while True:
        grown = set(found)
        for module, tree in candidates:
            resolver = _Resolver(tree, module, found)
            for name in resolver.module_names():
                if any(resolver.is_facade(v) for v in resolver.values(ast.Name(id=name), tree)):
                    grown.add(f"{module}.{name}")
        if grown == found:
            return found
        found = frozenset(grown)


def _may_pass_create(call: ast.Call) -> bool:
    """Whether *call* could hand a patch a ``create`` that is not literally ``False``.

    A ``create`` keyword that is not the literal ``False``, or three positional arguments
    or more to a callable spelled ``patch``, ``object`` or ``multiple`` -- where
    ``create`` would sit positionally. Only these calls are worth resolving.
    """
    for keyword in call.keywords:
        if keyword.arg == "create":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
    func = call.func
    spelled = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return len(call.args) >= 3 and spelled in ("patch", "object", "multiple")


def _create_true_patches_of_forwarded_names(source: str, module: str | None = None) -> list[_Hit]:
    """Every ``mock.patch`` of a forwarded name in *source* whose ``create`` is not
    literally ``False``.

    Any spelling the reader can resolve counts: ``patch``, ``patch.object`` and
    ``patch.multiple`` reached through any import alias, called or used as a decorator,
    with a positional or keyword target and attribute, a keyword ``create`` -- or a
    positional one to a callable spelled ``patch``, ``object`` or ``multiple`` -- and a
    string target built from the facade's name. A patch of the facade whose attribute it cannot
    resolve is a ``<dynamic>`` hit, never a pass. A name the facade binds itself is
    safe -- ``mock.patch`` sees it as local and writes the original back.
    """
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _may_pass_create(node)
    ]
    if not calls:
        return []
    resolver = _Resolver(tree, module, _facade_reexports())
    return [
        _Hit(resolver.function_of(call), name, call.lineno)
        for call in calls
        for name in resolver.hits(call)
    ]


def _patch_sources(*needles: str) -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every test module of the repository that names all *needles*.

    ``test/`` and every directory under ``src/`` whose name ends in ``tests`` (``tests``,
    ``container_tests``), read once as text and handed to the AST reader only when it
    names every one of *needles*: ``create`` and ``backup`` for a ``create=True`` patch of
    the facade, ``backup_parts`` for a write into a part.
    """
    paths = [*(_REPO_ROOT / "test").rglob("*.py")]
    paths += [path for path in (_REPO_ROOT / "src").rglob("*.py") if _in_a_test_directory(path)]
    for path in paths:
        if _NOT_SCANNED.intersection(path.relative_to(_REPO_ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if all(needle in text for needle in needles):
            yield path, text


#: Imports most reader cases share.
_CASE_IMPORTS = (
    "from unittest import mock\n" "from kiro_crew.apps.builtins.aws_control.backend import backup\n"
)

#: ``(id, source, module, expected names)`` for the reader: each form the guard must
#: catch beside a spelling of it the guard must leave alone. ``@EXP@`` is a forwarded
#: name, ``@BOUND@`` one the facade binds itself, ``@FACADE@`` the facade's dotted name.
_READER_CASES: list[tuple[str, str, str | None, list[str]]] = [
    (
        "patch.object",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "create=False",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', create=False)\n",
        None,
        [],
    ),
    ("no create", _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@')\n", None, []),
    (
        "create positionally",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', mock.DEFAULT, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "False positionally",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', mock.DEFAULT, None, False)\n",
        None,
        [],
    ),
    (
        "create not a literal",
        _CASE_IMPORTS + "flag = True\nmock.patch.object(backup, '@EXP@', create=flag)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a name the facade binds",
        _CASE_IMPORTS + "mock.patch.object(backup, '@BOUND@', create=True)\n",
        None,
        [],
    ),
    (
        "a name no module holds",
        _CASE_IMPORTS + "mock.patch.object(backup, '_redact_for_upload', create=True)\n",
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
        "another module's .backup",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(config.backup, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "patch as an alias",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nP.object(backup, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a local function named patch",
        "from kiro_crew.apps.builtins.aws_control.backend import backup\ndef patch(*a, **k):\n    return None\npatch.object(backup, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "mock as an alias",
        "from unittest import mock as M\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nM.patch.object(backup, '@EXP@', create=True)\n",
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
        "import unittest.mock\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nunittest.mock.patch.multiple(backup, create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a third-party mock",
        "import mock\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nmock.patch.object(backup, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "as a decorator",
        _CASE_IMPORTS
        + "@mock.patch.object(backup, '@EXP@', create=True)\ndef test_x(fake):\n    pass\n",
        None,
        ["@EXP@"],
    ),
    (
        "a decorator with create=False",
        _CASE_IMPORTS
        + "@mock.patch.object(backup, '@EXP@', create=False)\ndef test_x(fake):\n    pass\n",
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
        "from unittest import mock\nmock.patch('@FACADE@.storage.find_drive', create=True)\n",
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
        _CASE_IMPORTS + "mock.patch(f'{backup.__name__}.@EXP@', create=True)\n",
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
        "from unittest import mock\nFACADE = '@FACADE@'\nmock.patch(f'{FACADE}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another constant",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\nmock.patch(f'{OTHER}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a constant concatenated",
        "from unittest import mock\nFACADE = '@FACADE@'\nmock.patch(FACADE + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "__name__ concatenated",
        _CASE_IMPORTS + "mock.patch(backup.__name__ + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "another constant concatenated",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\nmock.patch(OTHER + '.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword target and attribute",
        _CASE_IMPORTS + "mock.patch.object(target=backup, attribute='@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "keyword target elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(target=config, attribute='@EXP@', create=True)\n",
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
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(backup, attr, create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an unresolved attribute, create=False",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(backup, attr, create=False)\n",
        None,
        [],
    ),
    (
        "a resolved local attribute",
        _CASE_IMPORTS
        + "def test_x():\n    name = '@EXP@'\n    mock.patch.object(backup, name, create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string it cannot finish",
        _CASE_IMPORTS
        + "def test_x(attr):\n    mock.patch(f'{backup.__name__}.{attr}', create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs in patch.multiple",
        _CASE_IMPORTS + "def test_x(kw):\n    mock.patch.multiple(backup, create=True, **kw)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\ndef test_x(kw):\n    mock.patch.multiple(config, create=True, **kw)\n",
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
        "from unittest import mock\nmock.patch.multiple('kiro_crew.config', create=True, @EXP@=None)\n",
        None,
        [],
    ),
    (
        "an assigned re-export",
        "from unittest import mock\nfrom kiro_crew.apps.builtins.aws_control import hooks\ndef test_x():\n    bk = hooks.backup_mod\n    mock.patch.object(bk, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an assigned other attribute",
        "from unittest import mock\nfrom kiro_crew.apps.builtins.aws_control import hooks\ndef test_x():\n    bk = hooks.backup_now\n    mock.patch.object(bk, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "another module's re-export",
        "from unittest import mock\nfrom kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod\nalias = routes_mod.backup_mod\nmock.patch.object(alias, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module",
        "import importlib\nfrom unittest import mock\nfacade = importlib.import_module('@FACADE@')\nmock.patch.object(facade, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module elsewhere",
        "import importlib\nfrom unittest import mock\nother = importlib.import_module('kiro_crew.config')\nmock.patch.object(other, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a chain of assignments",
        _CASE_IMPORTS + "a = backup\nb = a\nmock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a relative import in a package",
        "from unittest import mock\nfrom ..backend import backup\nmock.patch.object(backup, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.aws_control.tests.test_case",
        ["@EXP@"],
    ),
    (
        "a relative import elsewhere",
        "from unittest import mock\nfrom ..crew import backup\nmock.patch.object(backup, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.aws_control.tests.test_case",
        [],
    ),
]


def _case(template: str) -> str:
    return (
        template.replace("@EXP@", "_locked_state_update")
        .replace("@BOUND@", "_add_tree")
        .replace("@FACADE@", backup.__name__)
    )


class TestPatchSpellings:
    def test_the_case_names_are_what_they_claim(self) -> None:
        assert "_locked_state_update" in backup._EXPORTS
        assert "_add_tree" in vars(backup) and "_add_tree" not in backup._EXPORTS
        assert "_redact_for_upload" not in backup._EXPORTS and not hasattr(
            backup, "_redact_for_upload"
        )
        assert _facade_reexports() >= {
            "kiro_crew.apps.builtins.aws_control.hooks.backup_mod",
            "kiro_crew.apps.builtins.aws_control.backend.routes.backup_mod",
        }

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _READER_CASES],
        ids=[case[0] for case in _READER_CASES],
    )
    def test_the_reader_flags_every_spelling_and_only_those(
        self, source: str, module: str | None, expected: list[str]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that flagged a
        # safe spelling would stop a patch that undoes cleanly.
        hits = _create_true_patches_of_forwarded_names(_case(source), module)
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    def test_no_test_patches_a_forwarded_name_with_create_true(self) -> None:
        # ``mock.patch`` undoes a name the facade forwards by deleting it, and under
        # ``create=True`` the delete is the whole undo: the name is gone from every
        # engine module for the rest of the run, and a later test fails far from here.
        # The scan must find exactly the allowlisted premise case, so the allowlist can
        # neither hide a second site nor outlive the one it names.
        hits = [
            (path.relative_to(_REPO_ROOT).as_posix(), hit)
            for path, text in _patch_sources("create", "backup")
            for hit in _create_true_patches_of_forwarded_names(text, _module_name(path))
        ]
        found = {(path, hit.function) for path, hit in hits}
        unexpected = [
            f"{path}:{hit.line} {hit.function} patches {hit.name}"
            for path, hit in hits
            if (path, hit.function) not in _ALLOWED_CREATE_TRUE
        ]
        assert found == _ALLOWED_CREATE_TRUE, (
            "mock.patch(..., create=True) of a name backend.backup forwards to its owner "
            "deletes that name from every engine module when the patch exits. Drop "
            "create=True (the name exists) or patch it with monkeypatch.setattr: "
            f"{unexpected}; allowlisted but not found: {sorted(_ALLOWED_CREATE_TRUE - found)}"
        )


# ---------------------------------------------------------------------------
# A test never patches a part directly
# ---------------------------------------------------------------------------
# A part resolves a name through its own globals, and so does every part that imported
# it, so a write into one part reaches none of the other holders: the code under test
# can keep running the unpatched object while the test passes. Only a write through the
# facade reaches every holder, so a test patches the facade, never a part. The guard
# reads every test module the way the ``create=True`` guard does and fails on a write
# into the namespace of the ``backup_parts`` package or of a module in it, apart from
# the premise cases below.

_BACKEND_PACKAGE = _PARTS_PACKAGE.rpartition(".")[0]

#: The package and every module in it, by dotted name: the namespaces a write must not
#: land in.
_PART_NAMESPACES = frozenset({_PARTS_PACKAGE, *backup._PART_MODULES})

#: The deliberate writes into a part, keyed by file and the test enclosing them: the
#: premise cases, which show what such a write reaches and what it does not.
_ALLOWED_PART_PATCHES = frozenset(
    {
        (
            "test/test_aws_control_backup_composition_contract.py",
            "TestTheSurfaceSurvivesTheSplit."
            "test_an_exported_name_is_read_from_its_owner_on_every_access",
        ),
        (
            "test/test_aws_control_backup_composition_contract.py",
            "TestTheSurfaceSurvivesTheSplit."
            "test_a_name_a_part_rebinds_stays_live_through_the_facade",
        ),
    }
)

#: The ``MonkeyPatch`` methods and builtins that write or delete a module attribute.
_ATTRIBUTE_WRITERS = frozenset({"setattr", "delattr"})

#: The ``MonkeyPatch`` methods that write or delete an item of a mapping.
_ITEM_WRITERS = frozenset({"setitem", "delitem"})

#: The patch callable that writes items of a mapping.
_PATCH_DICT = "unittest.mock.patch.dict"


class _PartResolver(_Resolver):
    """A :class:`_Resolver` that also reads a part's ``__name__`` as its dotted name and
    ``sys.modules[<name>]`` as the module of that name.

    A parameter, an unpacking or ``:=`` target, a ``for`` or ``with`` target, an
    ``except`` name, a ``match`` capture and a nested ``def`` or ``class`` are local to
    their function, so each shadows a name an enclosing scope imports and denotes
    nothing the reader can follow. A comprehension variable is local to the
    comprehension, a scope the reader does not model, so it shadows nothing: a part
    named inside one still resolves to the part.
    """

    def __init__(self, tree: ast.Module, module: str | None, reexports: frozenset[str]) -> None:
        super().__init__(tree, module, reexports)
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                self._bind(node, node.arg, frozenset())
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.NamedExpr)):
                self._shadow(node, node.target)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        self._shadow(node, target)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                self._shadow(node, node.optional_vars)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                self._bind(node, node.name, frozenset())
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._bind(node, node.name, frozenset())

    def _shadow(self, statement: ast.AST, target: ast.expr) -> None:
        """Bind every name *target* stores to, in *statement*'s scope, to nothing."""
        if isinstance(target, ast.Name):
            self._bind(statement, target.id, frozenset())
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._shadow(statement, element)
        elif isinstance(target, ast.Starred):
            self._shadow(statement, target.value)

    def values(self, expr: ast.AST | None, scope: ast.AST) -> set[_Value]:
        found = super().values(expr, scope)
        if isinstance(expr, ast.Attribute) and expr.attr == "__name__":
            found |= {
                ("str", text)
                for kind, text in self.values(expr.value, scope)
                if kind == "path" and text in _PART_NAMESPACES
            }
        elif isinstance(expr, ast.Subscript):
            if ("path", "sys.modules") in self.values(expr.value, scope):
                found |= {
                    ("path", text) for kind, text in self.values(expr.slice, scope) if kind == "str"
                }
        return found


def _namespace_of(expr: ast.expr | None) -> ast.expr | None:
    """The module *expr* is the namespace dict of -- ``vars(module)`` or
    ``module.__dict__`` -- or ``None`` for any other mapping."""
    if isinstance(expr, ast.Attribute) and expr.attr == "__dict__":
        return expr.value
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "vars"
        and len(expr.args) == 1
        and not expr.keywords
    ):
        return expr.args[0]
    return None


def _written_targets(node: ast.AST, resolver: _Resolver) -> list[tuple[ast.expr | None, bool]]:
    """``(expression, names_an_attribute)`` for each namespace *node* writes into.

    The expression denotes the module written into, or is a dotted string; the flag says
    whether such a string names ``<module>.<attribute>`` rather than the module itself.
    """
    if isinstance(node, ast.Call):
        func = node.func
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        first = node.args[0] if node.args else None
        resolved = {text for _kind, text in resolver.values(func, resolver.scope_of(node))}
        for text in resolved & set(_PATCH_CALLABLES):
            return [(keywords.get("target", first), _PATCH_CALLABLES[text][0] == "patch")]
        if _PATCH_DICT in resolved:
            return [(_namespace_of(keywords.get("in_dict", first)), False)]
        # A ``MonkeyPatch`` is reached through a fixture the reader cannot follow, so
        # its methods are matched by name; the builtins are matched when not rebound.
        if isinstance(func, ast.Attribute) and func.attr in _ATTRIBUTE_WRITERS:
            return [(keywords.get("target", first), True)]
        if isinstance(func, ast.Attribute) and func.attr in _ITEM_WRITERS:
            return [(_namespace_of(keywords.get("dic", first)), False)]
        if isinstance(func, ast.Name) and func.id in _ATTRIBUTE_WRITERS and not resolved:
            return [(first, False)]
        return []
    if isinstance(node, (ast.Assign, ast.Delete)):
        targets = node.targets
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    else:
        return []
    found: list[tuple[ast.expr | None, bool]] = []
    while targets:
        target, *targets = targets
        if isinstance(target, (ast.Tuple, ast.List)):
            targets = [*target.elts, *targets]
        elif isinstance(target, ast.Starred):
            targets = [target.value, *targets]
        elif isinstance(target, ast.Attribute):
            found.append((target.value, False))
        elif isinstance(target, ast.Subscript):
            found.append((_namespace_of(target.value), False))
    return found


def _part_named(value: _Value, names_an_attribute: bool) -> str | None:
    """The part namespace *value* writes into, relative to ``backend``; ``None`` for any
    other.

    A module object, or a string naming one, is a part only when it IS the package or a
    module in it: an object a part holds is the same object every holder sees. A string
    naming ``<module>.<attribute>`` writes into the module before its last dot, and so
    does a string the reader cannot finish, whose hit ends in ``<dynamic>``.
    """
    kind, text = value
    if kind == "prefix" or (kind == "str" and names_an_attribute):
        namespace = text.rpartition(".")[0]
    else:
        namespace = text
    if namespace not in _PART_NAMESPACES:
        return None
    named = text[len(_BACKEND_PACKAGE) + 1 :]
    return named + _DYNAMIC if kind == "prefix" else named


def _direct_part_patches(source: str, module: str | None = None) -> list[_Hit]:
    """Every write into the namespace of a ``backup_parts`` module in *source* that the
    reader can resolve.

    A write is a ``patch``, ``patch.object`` or ``patch.multiple`` reached through any
    import alias, called or used as a decorator; a ``setattr`` or ``delattr``, the
    builtin or a ``MonkeyPatch`` method; an assignment, augmented assignment or ``del``
    of an attribute; or a write into the module's own dict, ``vars(module)`` or
    ``module.__dict__``, through ``patch.dict``, ``MonkeyPatch.setitem`` / ``delitem``
    or an item assignment. It is a hit when the module is the package or a module in
    it, spelled as a module object, as ``sys.modules[<its name>]``, or as a dotted
    string or one built from it, positional or by keyword. A hit names its target
    relative to ``backend``. A write into an object a part holds -- an attribute of a
    module, class, lock or table it defines or imports -- reaches every holder of that
    object and is not one. A method call on the namespace dict (``update``, ``pop``) is
    not read.
    """
    tree = ast.parse(source)
    resolver = _PartResolver(tree, module, _facade_reexports())
    hits: list[_Hit] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Call, ast.stmt)):
            continue
        scope = resolver.scope_of(node)
        for target, names_an_attribute in _written_targets(node, resolver):
            named = {
                _part_named(value, names_an_attribute) for value in resolver.values(target, scope)
            }
            hits += [
                _Hit(resolver.function_of(node), name, node.lineno)
                for name in sorted(name for name in named if name is not None)
            ]
    return sorted(hits, key=lambda hit: (hit.line, hit.name))


#: Imports most part-write cases share.
_PART_CASE_IMPORTS = (
    "import pytest\n"
    "from unittest import mock\n"
    "from kiro_crew.apps.builtins.aws_control.backend import backup\n"
    "from kiro_crew.apps.builtins.aws_control.backend.backup_parts import state\n"
)

#: ``(id, source, module, expected names)``: each write the guard must catch beside a
#: spelling of it the guard must leave alone. ``@PARTS@`` is the package's dotted name
#: and ``@FACADE@`` the facade's.
_PART_CASES: list[tuple[str, str, str | None, list[str]]] = [
    (
        "monkeypatch.setattr",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    monkeypatch.setattr(state, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "monkeypatch.setattr of the facade",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    monkeypatch.setattr(backup, '_state_path', None)\n",
        None,
        [],
    ),
    (
        "monkeypatch.setattr of a dotted string",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('@PARTS@.state._state_path', None)\n",
        None,
        ["backup_parts.state._state_path"],
    ),
    (
        "monkeypatch.setattr of the facade's dotted string",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('@FACADE@._state_path', None)\n",
        None,
        [],
    ),
    (
        "monkeypatch.setattr with a keyword target",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n"
        + "    monkeypatch.setattr(target=state, name='_state_path', value=None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "monkeypatch.delattr",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    monkeypatch.delattr(state, '_state_path')\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a MonkeyPatch context",
        _PART_CASE_IMPORTS
        + "def test_x():\n    with pytest.MonkeyPatch.context() as patched:\n"
        + "        patched.setattr(state, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch.object",
        _PART_CASE_IMPORTS + "mock.patch.object(state, '_state_path')\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch.object of the facade",
        _PART_CASE_IMPORTS + "mock.patch.object(backup, '_state_path')\n",
        None,
        [],
    ),
    (
        "patch.object with a keyword target",
        _PART_CASE_IMPORTS + "mock.patch.object(target=state, attribute='_state_path')\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch of a dotted string",
        "from unittest import mock\nmock.patch('@PARTS@.state._state_path')\n",
        None,
        ["backup_parts.state._state_path"],
    ),
    (
        "patch of the facade's dotted string",
        "from unittest import mock\nmock.patch('@FACADE@._state_path')\n",
        None,
        [],
    ),
    (
        "patch of a name that only starts like the package",
        "from unittest import mock\nmock.patch('@PARTS@x.state._state_path')\n",
        None,
        [],
    ),
    (
        "patch.multiple",
        _PART_CASE_IMPORTS + "mock.patch.multiple(state, _state_path=None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch.multiple of a dotted string",
        "from unittest import mock\nmock.patch.multiple('@PARTS@.state', _state_path=None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "as a decorator",
        _PART_CASE_IMPORTS
        + "@mock.patch.object(state, '_state_path')\ndef test_x(fake):\n    pass\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch as an alias",
        "from unittest.mock import patch as P\n"
        "from kiro_crew.apps.builtins.aws_control.backend.backup_parts import state\n"
        "P.object(state, '_state_path')\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a third-party mock",
        "import mock\n"
        "from kiro_crew.apps.builtins.aws_control.backend.backup_parts import state\n"
        "mock.patch.object(state, '_state_path')\n",
        None,
        [],
    ),
    (
        "a part under an alias",
        "from kiro_crew.apps.builtins.aws_control.backend.backup_parts import state as st\n"
        "def test_x(monkeypatch):\n    monkeypatch.setattr(st, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a part through its package",
        "from kiro_crew.apps.builtins.aws_control.backend import backup_parts\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(backup_parts.state, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "the package itself",
        "from kiro_crew.apps.builtins.aws_control.backend import backup_parts\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(backup_parts, '_FACADE_MODULE', 'x')\n",
        None,
        ["backup_parts"],
    ),
    (
        "a relative import in a package",
        "from ..backend.backup_parts import state\n"
        "def test_x(monkeypatch):\n    monkeypatch.setattr(state, '_state_path', None)\n",
        "kiro_crew.apps.builtins.aws_control.tests.test_case",
        ["backup_parts.state"],
    ),
    (
        "a relative import elsewhere",
        "from ..crew.backup_parts import state\n"
        "def test_x(monkeypatch):\n    monkeypatch.setattr(state, '_state_path', None)\n",
        "kiro_crew.apps.builtins.aws_control.tests.test_case",
        [],
    ),
    (
        "import_module",
        "import importlib\n"
        "def test_x(monkeypatch):\n"
        "    part = importlib.import_module('@PARTS@.state')\n"
        "    monkeypatch.setattr(part, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "an f-string of a part's __name__",
        _PART_CASE_IMPORTS + "mock.patch(f'{state.__name__}._state_path')\n",
        None,
        ["backup_parts.state._state_path"],
    ),
    (
        "an f-string of the facade's __name__",
        _PART_CASE_IMPORTS + "mock.patch(f'{backup.__name__}._state_path')\n",
        None,
        [],
    ),
    (
        "a constant concatenated",
        "from unittest import mock\nPART = '@PARTS@.state'\nmock.patch(PART + '._state_path')\n",
        None,
        ["backup_parts.state._state_path"],
    ),
    (
        "an f-string it cannot finish",
        "from unittest import mock\n"
        "def test_x(attr):\n    mock.patch(f'@PARTS@.state.{attr}')\n",
        None,
        ["backup_parts.state.<dynamic>"],
    ),
    (
        "the builtin setattr",
        _PART_CASE_IMPORTS + "setattr(state, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "the builtin delattr",
        _PART_CASE_IMPORTS + "delattr(state, '_state_path')\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "an assignment",
        _PART_CASE_IMPORTS + "state._state_path = None\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "an unpacking assignment",
        _PART_CASE_IMPORTS + "x, state._state_path = None, None\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "an augmented assignment",
        _PART_CASE_IMPORTS + "state._counter += 1\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a del",
        _PART_CASE_IMPORTS + "del state._state_path\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a read",
        _PART_CASE_IMPORTS + "path = state._state_path\n",
        None,
        [],
    ),
    (
        "an item of a part's table",
        _PART_CASE_IMPORTS + "state._TABLE['key'] = None\n",
        None,
        [],
    ),
    (
        "patch.dict of a part's table",
        _PART_CASE_IMPORTS + "mock.patch.dict(state._TABLE, {'key': None})\n",
        None,
        [],
    ),
    (
        "a local shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    state = object()\n"
        + "    monkeypatch.setattr(state, '_state_path', None)\n",
        None,
        [],
    ),
    (
        "a parameter shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch, state):\n    monkeypatch.setattr(state, '_state_path', None)\n",
        None,
        [],
    ),
    (
        "a loop target shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x():\n    for state in ():\n        setattr(state, '_state_path', None)\n",
        None,
        [],
    ),
    (
        "another module",
        "from kiro_crew import config\n"
        "def test_x(monkeypatch):\n    monkeypatch.setattr(config, '_state_path', None)\n",
        None,
        [],
    ),
    (
        "a comprehension variable shadows nothing outside it",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch, tmp_path):\n"
        + "    assert all(state for state in tmp_path.iterdir())\n"
        + "    monkeypatch.setattr(state, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a name read in a loop target shadows nothing",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch, rows):\n    for rows[state] in ():\n        pass\n"
        + "    monkeypatch.setattr(state, '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a with target shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch, path):\n    with open(path) as state:\n"
        + "        monkeypatch.setattr(state, 'name', None)\n",
        None,
        [],
    ),
    (
        "an unpacking target shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    state, code = object(), 0\n"
        + "    monkeypatch.setattr(state, 'name', None)\n",
        None,
        [],
    ),
    (
        "a := target shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch, make):\n    if (state := make()):\n"
        + "        monkeypatch.setattr(state, 'name', None)\n",
        None,
        [],
    ),
    (
        "a nested def shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    def state():\n        pass\n"
        + "    monkeypatch.setattr(state, '__doc__', None)\n",
        None,
        [],
    ),
    (
        "a match capture shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch, value):\n    match value:\n        case [state]:\n"
        + "            monkeypatch.setattr(state, 'name', None)\n",
        None,
        [],
    ),
    (
        "an except name shadowing the part",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    try:\n        pass\n    except OSError as state:\n"
        + "        monkeypatch.setattr(state, 'errno', None)\n",
        None,
        [],
    ),
    (
        "sys.modules of a part",
        "import sys\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(sys.modules['@PARTS@.state'], '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "sys.modules of the facade",
        "import sys\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(sys.modules['@FACADE@'], '_state_path', None)\n",
        None,
        [],
    ),
    (
        "monkeypatch.setitem of vars(part)",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    monkeypatch.setitem(vars(state), '_state_path', None)\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "monkeypatch.delitem of part.__dict__",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    monkeypatch.delitem(state.__dict__, '_state_path')\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "monkeypatch.setitem of a part's table",
        _PART_CASE_IMPORTS
        + "def test_x(monkeypatch):\n    monkeypatch.setitem(state._TABLE, 'key', None)\n",
        None,
        [],
    ),
    (
        "patch.dict of part.__dict__",
        _PART_CASE_IMPORTS + "mock.patch.dict(state.__dict__, {'_state_path': None})\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch.dict of vars(part) by keyword",
        _PART_CASE_IMPORTS + "mock.patch.dict(in_dict=vars(state), values={})\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "patch.dict of the facade's namespace",
        _PART_CASE_IMPORTS + "mock.patch.dict(vars(backup), {'_state_path': None})\n",
        None,
        [],
    ),
    (
        "an item of vars(part)",
        _PART_CASE_IMPORTS + "vars(state)['_state_path'] = None\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "a del of an item of part.__dict__",
        _PART_CASE_IMPORTS + "del state.__dict__['_state_path']\n",
        None,
        ["backup_parts.state"],
    ),
    (
        "an attribute of a module a part imports",
        "from kiro_crew.apps.builtins.aws_control.backend.backup_parts import retention\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(retention.storage, 'delete_key', None)\n",
        None,
        [],
    ),
    (
        "patch.object of an object a part holds",
        _PART_CASE_IMPORTS + "mock.patch.object(state._TABLE, 'get')\n",
        None,
        [],
    ),
    (
        "an attribute assignment into an object a part holds",
        _PART_CASE_IMPORTS + "state._TABLE.limit = 1\n",
        None,
        [],
    ),
    (
        "a dotted string into a module a part imports",
        "from unittest import mock\nmock.patch('@PARTS@.retention.storage.delete_key')\n",
        None,
        [],
    ),
    (
        "a dotted string rebinding a part's import",
        "from unittest import mock\nmock.patch('@PARTS@.retention.storage')\n",
        None,
        ["backup_parts.retention.storage"],
    ),
    (
        "an f-string of an imported module's __name__",
        "from unittest import mock\n"
        "from kiro_crew.apps.builtins.aws_control.backend.backup_parts import retention\n"
        "mock.patch(f'{retention.storage.__name__}.delete_key')\n",
        None,
        [],
    ),
]


def _part_case(template: str) -> str:
    return template.replace("@PARTS@", _PARTS_PACKAGE).replace("@FACADE@", backup.__name__)


class TestNoTestPatchesAPart:
    def test_the_namespaces_are_the_package_and_its_modules(self) -> None:
        # A part missing from the set is a part the guard cannot see; a module a part
        # imports is no part, whichever part's attribute reaches it.
        assert _PART_NAMESPACES == {_PARTS_PACKAGE, *(part.__name__ for part in PARTS)}
        assert retention.storage.__name__ not in _PART_NAMESPACES

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _PART_CASES],
        ids=[case[0] for case in _PART_CASES],
    )
    def test_the_reader_flags_every_write_into_a_part_and_only_those(
        self, source: str, module: str | None, expected: list[str]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that flagged a
        # write through the facade would stop the one patch form that reaches every holder.
        hits = _direct_part_patches(_part_case(source), module)
        assert [hit.name for hit in hits] == expected

    def test_no_test_patches_a_part_directly(self) -> None:
        # The scan must find exactly the allowlisted premise cases, so the allowlist can
        # neither hide another site nor outlive the ones it names.
        hits = [
            (path.relative_to(_REPO_ROOT).as_posix(), hit)
            for path, text in _patch_sources("backup_parts")
            for hit in _direct_part_patches(text, _module_name(path))
        ]
        found = {(path, hit.function) for path, hit in hits}
        unexpected = [
            f"{path}:{hit.line} {hit.function} writes into {hit.name}"
            for path, hit in hits
            if (path, hit.function) not in _ALLOWED_PART_PATCHES
        ]
        assert found == _ALLOWED_PART_PATCHES, (
            "a write into a backup_parts module reaches no other module that holds the "
            "name, so the code under test can keep running the unpatched object. Patch "
            "backend.backup instead, which writes every holder: "
            f"{unexpected}; allowlisted but not found: {sorted(_ALLOWED_PART_PATCHES - found)}"
        )
