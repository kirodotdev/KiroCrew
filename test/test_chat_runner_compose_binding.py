"""``chat_turn.compose`` binds on the runner what its owners define, and nothing else.

The runner keeps no by-name re-export of most owner names: :func:`chat_turn.compose`
binds every name an owner binds at runtime onto ``kiro_crew.dashboard.chat_runner``
(``_bind``), so ``chat_runner.<name>`` resolves for each owner function, class and
constant, and a test's patch of it reaches every function composed onto the runner. A
name the runner and an owner both bind must be ONE object, or the import fails. The
owners are the package's modules, found by :func:`chat_turn.owner_modules`.

What this pins:

* every name an owner binds is bound on the runner, as the same object;
* a name bound to a different object in the namespace and an owner makes ``compose``
  raise instead of keeping either;
* the owners ``compose`` sees are exactly the package's modules;
* every name the runner itself imports is read by something: an owner function, the
  runner's own code, a test's patch of it, or the base-names surface. The runner is
  exempt from F401 (``setup.cfg``): pyflakes reads only its own text, and an import
  whose only reader moved to an owner is read in another file.

Each check has a self-test on synthetic modules, so a check that stopped seeing
anything fails here rather than passing.
"""

from __future__ import annotations

import ast
import dis
import importlib.util
import pkgutil
import textwrap
import types
from pathlib import Path

import pytest
from test_chat_runner_composition_contract import _BASE_NAMES, _facade_patched_names

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.dashboard import chat_turn

_OWNER_DIR = Path(chat_turn.__file__).resolve().parent


def _bound_names(module: types.ModuleType) -> dict[str, object]:
    """What ``compose`` binds from *module*: every runtime binding but dunders, modules
    and ``TYPE_CHECKING``."""
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("__")
        and name != "TYPE_CHECKING"
        and not isinstance(value, types.ModuleType)
    }


def _unbound_on(namespace: dict[str, object], owners: list[types.ModuleType]) -> list[str]:
    """Names an owner binds that *namespace* lacks or holds as a different object."""
    missing = []
    for owner in owners:
        for name, value in _bound_names(owner).items():
            if namespace.get(name, missing) is not value:
                missing.append(f"{owner.__name__.rsplit('.', 1)[-1]}.{name}")
    return missing


def _write_package(tmp_path: Path, name: str, modules: dict[str, str]) -> types.ModuleType:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for stem, source in modules.items():
        (pkg / f"{stem}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        name, pkg / "__init__.py", submodule_search_locations=[str(pkg)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the binding ───────────────────────────────────────────────────────────────


def test_every_name_an_owner_binds_is_the_runners() -> None:
    owners = list(chat_turn.owner_modules())
    assert len(owners) >= 13
    assert _unbound_on(vars(cr), owners) == []


def test_the_binding_check_sees_a_missing_name(tmp_path: Path) -> None:
    owner = _load(
        _write_owner(tmp_path, "binding_probe_owner", "VALUE = 1\ndef f():\n    return VALUE\n"),
        "binding_probe_owner",
    )
    assert _unbound_on({"VALUE": 1}, [owner]) == ["binding_probe_owner.f"]


def _write_owner(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_compose_binds_an_owners_names_on_the_namespace(tmp_path: Path) -> None:
    owner = _load(
        _write_owner(
            tmp_path,
            "binding_probe_definer",
            "from os.path import join\nLIMIT = 3\nclass Box:\n    pass\ndef f():\n    return LIMIT\n",
        ),
        "binding_probe_definer",
    )
    namespace: dict[str, object] = {"__name__": "binding_probe_host"}
    chat_turn.compose(namespace, (owner,))
    assert namespace["LIMIT"] == 3 and namespace["Box"] is owner.Box
    assert namespace["f"] is owner.f and owner.f.__globals__ is namespace
    assert namespace["join"] is owner.join
    assert "os" not in namespace and "__file__" not in namespace


def test_compose_refuses_two_objects_under_one_name(tmp_path: Path) -> None:
    owner = _load(
        _write_owner(tmp_path, "binding_probe_collider", "LIMIT = 3\n"), "binding_probe_collider"
    )
    namespace: dict[str, object] = {"__name__": "binding_probe_host", "LIMIT": 4}
    with pytest.raises(ImportError, match="different objects"):
        chat_turn.compose(namespace, (owner,))
    assert namespace["LIMIT"] == 4


# ── the owner set ─────────────────────────────────────────────────────────────


def _owner_names(package: types.ModuleType) -> set[str]:
    return {info.name for info in pkgutil.iter_modules(package.__path__)}


def test_compose_sees_exactly_the_packages_modules() -> None:
    assert {m.__name__.rsplit(".", 1)[-1] for m in chat_turn.owner_modules()} == _owner_names(
        chat_turn
    )


def test_the_owner_set_check_sees_an_unexpected_owner(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "binding_probe_pkg", {"alpha": "A = 1\n", "beta": "B = 2\n"})
    assert _owner_names(package) - {"alpha"} == {"beta"}


# ── every runner import is read ───────────────────────────────────────────────


def _code_globals(code: types.CodeType) -> set[str]:
    names = {
        i.argval
        for i in dis.get_instructions(code)
        if i.opname in ("LOAD_GLOBAL", "LOAD_NAME", "STORE_GLOBAL", "LOAD_FROM_DICT_OR_GLOBALS")
    }
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _code_globals(const)
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _annotations(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = node.args
        every = (
            args.posonlyargs
            + args.args
            + args.kwonlyargs
            + [a for a in (args.vararg, args.kwarg) if a]
        )
        return [a.annotation for a in every if a.annotation is not None] + (
            [node.returns] if node.returns is not None else []
        )
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    return []


def _names_in_annotation(annotation: ast.expr) -> set[str]:
    """The names a (possibly string) annotation reads: a type checker resolves them."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return set()
    return {n.id for n in ast.walk(annotation) if isinstance(n, ast.Name)}


def _unread_imports(source: str, reader_sources: list[str], keep: set[str]) -> list[str]:
    """Names *source* imports that neither it nor any of *reader_sources* reads."""
    imported = _imported_names(ast.parse(source)) - {"annotations"}
    read: set[str] = set()
    for text in [source, *reader_sources]:
        read |= _code_globals(compile(text, "<composed>", "exec"))
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Name):
                read.add(node.id)
            for annotation in _annotations(node):
                read |= _names_in_annotation(annotation)
    return sorted(imported - read - keep)


def _unread_local_imports(source: str) -> list[str]:
    """``function:name`` for each import inside a function that nothing in that function
    (its nested scopes included) reads."""
    found = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = [
            node
            for node in ast.walk(fn)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and not any(
                node in ast.walk(inner)
                for inner in ast.walk(fn)
                if inner is not fn and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        ]
        read = {
            n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        for node in own:
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name not in read:
                    found.append(f"{fn.name}:{name}")
    return sorted(found)


def test_the_local_import_check_sees_a_dead_one() -> None:
    source = "def f():\n    import json\n    from os import sep\n    return sep\n"
    assert _unread_local_imports(source) == ["f:json"]
    nested = "def f():\n    import json\n    def g():\n        return json\n    return g\n"
    assert _unread_local_imports(nested) == []


def _imported_from_the_runner(sources: list[str]) -> set[str]:
    """The names *sources* import from ``kiro_crew.dashboard.chat_runner`` by name."""
    found = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module == cr.__name__:
                found |= {alias.name for alias in node.names}
    return found


def test_the_runner_importer_scan_sees_a_by_name_import() -> None:
    sources = [
        "from kiro_crew.dashboard.chat_runner import a, b as c\n",
        "from kiro_crew.dashboard import chat_runner\nimport kiro_crew.dashboard.chat_runner\n",
    ]
    assert _imported_from_the_runner(sources) == {"a", "b"}


def test_every_name_the_runner_imports_is_read() -> None:
    importers = [
        path.read_text(encoding="utf-8")
        for path in sorted(
            (Path(__file__).resolve().parents[1] / "src" / "kiro_crew").rglob("*.py")
        )
        if path.name != "chat_runner.py" and "chat_turn" not in path.parts
    ]
    keep = set(_BASE_NAMES) | _facade_patched_names() | _imported_from_the_runner(importers)
    runner = Path(cr.__file__).read_text(encoding="utf-8")
    owners = [
        Path(owner.__file__).read_text(encoding="utf-8") for owner in chat_turn.owner_modules()
    ]
    assert _unread_imports(runner, owners, keep) == []
    assert _unread_local_imports(runner) == []


def test_the_import_read_check_sees_a_dead_import() -> None:
    host = "import json\nfrom os.path import join, sep\ndef f():\n    return join\n"
    assert _unread_imports(host, [], set()) == ["json", "sep"]
    assert _unread_imports(host, ["def g():\n    return sep\n"], set()) == ["json"]
