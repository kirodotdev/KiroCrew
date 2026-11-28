"""Which names a ``src/`` module offers from a changed module, read from its source.

``scripts/run_scoped_tests.py`` selects a test that reaches a changed module
through a facade: a ``src/`` module that offers the changed module's names under
its own path. The runner walks ``src/`` and selects the tests; this module parses
one source file at a time, each read through the runner's own reader. It uses the
standard library only, because ``scripts/leaf_test_scope.py`` loads the runner,
and so this module, in CI's dependency-free ``changes`` job. It imports nothing
from the runner, which imports it at load time.

A module offers an owner's names in four ways, and each is read here:

* it imports them and keeps them although unused, which flake8 allows through a
  ``# noqa`` that suppresses F401 or a ``per-file-ignores`` entry in ``setup.cfg``;
* it imports them and lists them in its ``__all__``;
* it maps the owner's dotted name to a tuple of names in a dict literal, as
  ``_EXPORTS_BY_OWNER`` and ``_FORWARDS_BY_OWNER`` do;
* it is a mirror or compose facade, which serves every name the owner defines:
  it imports ``kiro_crew._facade_mirror`` or ``kiro_crew._facade_compose`` and
  lists the owner in a tuple or list, by its dotted name or as the module it
  imported, or it hands its ``globals()`` to a package whose ``__init__`` imports
  one of them, which composes every submodule of that package onto it.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import functools
import posixpath
import re
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NamedTuple

SRC_ROOT = "src"
_FACADE_HELPERS = frozenset({"kiro_crew._facade_mirror", "kiro_crew._facade_compose"})
_HELPER_MODULE_NAMES = tuple(sorted(helper.rsplit(".", 1)[-1] for helper in _FACADE_HELPERS))
_PUBLIC_NAMES = "__all__"
_MODULE_NAMESPACE = "globals"
_UNUSED_IMPORT = "F401"
_FLAKE8_CONFIG = "setup.cfg"
# flake8's own inline pattern (``flake8.defaults.NOQA_INLINE_REGEXP``), code separator
# and ``per-file-ignores`` tokens (``flake8.utils._FILE_LIST_TOKEN_TYPES``, tried in
# that order), so a comment or an entry counts here exactly when flake8 honours it.
_NOQA = re.compile(r"# noqa(?::[\s]?(?P<codes>([A-Z]+[0-9]+(?:[,\s]+)?)+))?", re.IGNORECASE)
_CODE_SEPARATOR = re.compile(r"[,\s]")
_MAPPING_TOKEN = re.compile(
    r"(?P<code>[A-Z]+[0-9]*(?=$|\s|,))"
    r"|(?P<file>[^\s:,]+)"
    r"|(?P<colon>\s*:\s*)"
    r"|(?P<separator>\s*,\s*|\s+)"
)
_COMPOUND_STATEMENTS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
)


def module_of(rel: str) -> str | None:
    """The dotted module a ``src/`` Python path defines, or None for anything else."""
    p = PurePosixPath(rel)
    if p.suffix != ".py" or not p.parts or p.parts[0] != SRC_ROOT:
        return None
    parts = list(p.with_suffix("").parts[1:])
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def defined_names(text: str) -> frozenset[str]:
    """Names a module's own top level defines: functions, classes and assigned names.

    A name bound under a module-level ``if``, ``try``, ``with`` or loop counts, as the
    mirror counts it. Imported names do not: they are another module's definitions.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return frozenset()
    names: set[str] = set()
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(name for target in node.targets for name in _bound_names(target))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names.update(_bound_names(node.target))
        elif isinstance(node, _COMPOUND_STATEMENTS):
            pending.extend(
                child
                for child in ast.iter_child_nodes(node)
                if isinstance(child, (ast.stmt, ast.ExceptHandler))
            )
    return frozenset(name for name in names if not (name.startswith("__") and name.endswith("__")))


def _bound_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _bound_names(element)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


@dataclass(frozen=True)
class ChangedModules:
    """The changed ``src/`` modules one selection asks about, and what their facades need.

    ``defines`` maps each module's dotted name to the names it defines, which a mirror
    or compose facade serves whole. ``composers`` holds the packages above them whose
    ``__init__`` imports a facade helper, so that a module handing its ``globals()``
    to one is composed from every submodule of it. ``file_wide_keeps`` holds the
    ``per-file-ignores`` patterns that suppress F401.
    """

    root: Path
    defines: Mapping[str, frozenset[str]]
    composers: frozenset[str]
    file_wide_keeps: tuple[str, ...]

    @classmethod
    def read(
        cls, root: Path, paths: Collection[str], read_text: Callable[[str], str]
    ) -> ChangedModules:
        """What the scan needs for the changed ``paths``, each file read with ``read_text``.

        A deleted module defines nothing left to read.
        """
        defines = {
            owner: defined_names(read_text(path)) if (root / path).is_file() else frozenset()
            for path in paths
            if (owner := module_of(path))
        }
        packages = {above for owner in defines for above in _enclosing_packages(owner)}
        composers = frozenset(
            package
            for package in packages
            if (root / _init_of(package)).is_file()
            and _imports_a_helper(_init_of(package), read_text(_init_of(package)))
        )
        config = read_text(_FLAKE8_CONFIG) if (root / _FLAKE8_CONFIG).is_file() else ""
        return cls(root, defines, composers, files_keeping_unused_imports(config))

    def might_offer(self, rel: str, text: str) -> bool:
        """Could the module at ``rel`` offer a changed module's names? A text test only.

        It never says no where :meth:`offered_names` would find a name, because each
        way of offering leaves its own trace in the text:

        * a kept import spells ``<owner> import`` or ``.<last component> import``, or
          is a ``from .`` import inside the owner's package, and the module carries a
          ``# noqa``, a file-wide F401 ignore or ``__all__``;
        * a name table or a mirror's owner list quotes the owner's dotted name;
        * a mirror or compose facade names a facade helper and the owner's last
          component, or names a composing package above the owner and ``globals()``.
        """
        if f"{_MODULE_NAMESPACE}()" in text and any(
            _traces_of(package).last in text and self._composes_an_owner(package)
            for package in self.composers
        ):
            return True
        is_facade = any(helper in text for helper in _HELPER_MODULE_NAMES)
        keeps_imports: bool | None = None
        for owner in self.defines:
            traces = _traces_of(owner)
            inside_package = rel.startswith(traces.package_dir)
            # The cheap gate first: every trace but a bare `from .` spells the last component.
            named = traces.last in text
            if not named and not inside_package:
                continue
            if named and (is_facade or any(quoted in text for quoted in traces.quoted)):
                return True
            if not (
                any(spelling in text for spelling in traces.imports)
                or (inside_package and "from ." in text)
            ):
                continue
            if keeps_imports is None:
                keeps_imports = (
                    _NOQA.search(text) is not None
                    or _PUBLIC_NAMES in text
                    or _keeps_every_import(rel, self.file_wide_keeps)
                )
            if keeps_imports:
                return True
        return False

    def offered_names(self, rel: str, text: str) -> set[str]:
        """Names the module at ``rel`` offers from the changed modules.

        A plain import the module only uses itself is not an offer: the facade does not
        hand that name to anyone.
        """
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return set()
        owners = self.defines
        lines = text.splitlines()
        own = module_of(rel) or ""
        package = (
            own.split(".") if PurePosixPath(rel).name == "__init__.py" else own.split(".")[:-1]
        )
        keeps_every_import = _keeps_every_import(rel, self.file_wide_keeps)
        literal_names = {"__package__": ".".join(package), "__name__": own}
        names: set[str] = set()
        imported_from_owner: set[str] = set()
        public: set[str] = set()
        served: set[str] = set()
        bindings: dict[str, str] = {}
        listed: set[str] = set()
        composer_bindings: dict[str, str] = {}
        namespace_targets: set[str] = set()
        is_facade = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _imported_module(package, node)
                span = "\n".join(lines[node.lineno - 1 : node.end_lineno or node.lineno])
                for alias in node.names:
                    dotted = f"{module}.{alias.name}"
                    binding = alias.asname or alias.name
                    bindings[binding] = dotted
                    is_facade = is_facade or module in _FACADE_HELPERS or dotted in _FACADE_HELPERS
                    if dotted in self.composers:
                        composer_bindings[binding] = dotted
                    elif module in self.composers:
                        composer_bindings[binding] = module
                    # `from pkg import sub` binds a submodule, which the changed
                    # package's own source does not define.
                    if (
                        module in owners
                        and dotted not in owners
                        and alias.name != "*"
                        and not _is_source_module(self.root, dotted)
                    ):
                        imported_from_owner.add(binding)
                        if keeps_every_import or _keeps_unused_import(span):
                            names.add(binding)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    head = alias.name.split(".")[0]
                    bindings[alias.asname or head] = alias.name if alias.asname else head
                    is_facade = is_facade or alias.name in _FACADE_HELPERS
                    if alias.name in self.composers:
                        composer_bindings[alias.asname or alias.name] = alias.name
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == _PUBLIC_NAMES for t in targets):
                    public.update(_strings(node.value))
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in owners:
                        names.update(s for s in _strings(value) if s.isidentifier())
            elif isinstance(node, (ast.Tuple, ast.List)):
                # A facade's owner list: dotted names (a mirror's) or modules (a compose's).
                for element in node.elts:
                    literal = _literal_text(element, literal_names)
                    if literal in owners:
                        served.add(literal)
                    elif literal is None and (spelled := _dotted(element)):
                        listed.add(spelled)
            elif isinstance(node, ast.Call) and _hands_over_its_namespace(node):
                target = _dotted(node.func)
                if target:
                    namespace_targets.update({target, target.rsplit(".", 1)[0]})
        names.update(public & imported_from_owner)
        for spelled in listed:
            head, _, rest = spelled.partition(".")
            module = bindings.get(head)
            if module is not None and rest:
                module = f"{module}.{rest}"
            if module is not None and module in owners:
                served.add(module)
        if is_facade:
            for owner in served:
                names.update(owners[owner])
        for target in namespace_targets & set(composer_bindings):
            composer = composer_bindings[target]
            for owner, defined in owners.items():
                if owner.startswith(f"{composer}."):
                    names.update(defined)
        return names

    def _composes_an_owner(self, package: str) -> bool:
        return any(owner.startswith(f"{package}.") for owner in self.defines)


class _OwnerTraces(NamedTuple):
    last: str
    quoted: tuple[str, ...]
    imports: tuple[str, ...]
    package_dir: str


@functools.lru_cache(maxsize=None)
def _traces_of(owner: str) -> _OwnerTraces:
    """The text one module leaves in a module that offers its names."""
    last = owner.rsplit(".", 1)[-1]
    return _OwnerTraces(
        last=last,
        quoted=(f'"{owner}"', f"'{owner}'"),
        imports=(f"{owner} import", f".{last} import"),
        package_dir=f"{SRC_ROOT}/{owner.replace('.', '/')}/",
    )


def _enclosing_packages(owner: str) -> Iterator[str]:
    parts = owner.split(".")
    for end in range(len(parts) - 1, 0, -1):
        yield ".".join(parts[:end])


def _init_of(package: str) -> str:
    return f"{SRC_ROOT}/{package.replace('.', '/')}/__init__.py"


def _imports_a_helper(init: str, text: str) -> bool:
    """Does the package ``__init__`` at ``init`` import a facade helper?"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    package = (module_of(init) or "").split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _imported_module(package, node)
            if module in _FACADE_HELPERS or any(
                f"{module}.{alias.name}" in _FACADE_HELPERS for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name in _FACADE_HELPERS for alias in node.names):
                return True
    return False


def _hands_over_its_namespace(call: ast.Call) -> bool:
    """Is ``globals()`` one of the call's arguments?"""
    return any(
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Name)
        and argument.func.id == _MODULE_NAMESPACE
        and not argument.args
        for argument in call.args
    )


def _dotted(node: ast.expr) -> str | None:
    """The dotted name a name or attribute chain spells, or None for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _imported_module(package: list[str], node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, node.module] if node.module else base)


def _suppresses_unused_import(codes: Collection[str]) -> bool:
    """flake8's rule: a listed code suppresses every code it begins."""
    return _UNUSED_IMPORT.startswith(tuple(codes))


def _keeps_unused_import(span: str) -> bool:
    """Does flake8 read the ``# noqa`` in ``span`` as suppressing F401?

    flake8 searches a statement's lines for its first ``# noqa``, and a bare one
    suppresses every code.
    """
    match = _NOQA.search(span)
    if match is None:
        return False
    codes = match.group("codes")
    return codes is None or _suppresses_unused_import(_codes(codes))


def _keeps_every_import(rel: str, file_wide_keeps: Collection[str]) -> bool:
    """Does a ``per-file-ignores`` pattern suppress F401 for the whole file at ``rel``?

    flake8 matches every pattern against the file's name and against its path.
    """
    name = PurePosixPath(rel).name
    return any(
        fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern)
        for pattern in file_wide_keeps
    )


def files_keeping_unused_imports(config_text: str) -> tuple[str, ...]:
    """The ``per-file-ignores`` patterns of a ``setup.cfg``'s ``[flake8]`` that suppress F401.

    The entry is read with flake8's grammar: lists of file patterns, each followed by
    ``:`` and a list of codes, items separated by commas or whitespace. A pattern
    holding a ``/`` is resolved against the config's directory, as flake8 resolves it.
    """
    config = configparser.RawConfigParser()
    config.read_string(config_text)
    entry = config.get("flake8", "per-file-ignores", fallback="") or config.get(
        "flake8", "per_file_ignores", fallback=""
    )
    return tuple(
        posixpath.normpath(pattern) if "/" in pattern else pattern
        for pattern, codes in _files_to_codes(entry)
        if _suppresses_unused_import(codes)
    )


def _files_to_codes(entry: str) -> list[tuple[str, tuple[str, ...]]]:
    """Pair each file pattern of a ``per-file-ignores`` entry with the codes listed for it."""
    pairs: list[tuple[str, tuple[str, ...]]] = []
    files: list[str] = []
    codes: list[str] = []
    after_colon = False
    for match in _MAPPING_TOKEN.finditer(entry):
        kind, token = match.lastgroup, match.group().strip()
        if kind == "separator":
            continue
        if not after_colon:
            if kind == "colon":
                after_colon = True
            else:
                files.append(token)
        elif kind == "code":
            codes.append(token)
        elif kind == "file":
            pairs.extend((pattern, tuple(codes)) for pattern in files)
            files, codes, after_colon = [token], [], False
    pairs.extend((pattern, tuple(codes)) for pattern in files)
    return pairs


def _codes(text: str) -> tuple[str, ...]:
    return tuple(code.strip() for code in _CODE_SEPARATOR.split(text) if code.strip())


def _literal_text(node: ast.expr, names: Mapping[str, str]) -> str | None:
    """A string constant's value, or an f-string's when it formats only ``names``."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif (
            isinstance(value, ast.FormattedValue)
            and isinstance(value.value, ast.Name)
            and value.value.id in names
            and value.conversion == -1
            and value.format_spec is None
        ):
            parts.append(names[value.value.id])
        else:
            return None
    return "".join(parts)


def _strings(node: ast.expr | None) -> Iterator[str]:
    """The string constants a tuple or list literal holds; nothing for anything else."""
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                yield element.value


def _is_source_module(root: Path, dotted: str) -> bool:
    """Is ``dotted`` a module or package under ``src/``?"""
    base = root / SRC_ROOT / Path(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()
