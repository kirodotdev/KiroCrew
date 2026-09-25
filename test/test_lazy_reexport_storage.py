"""A lazily re-exported name has one storage location, in every package that has one.

Five packages re-export their public surface through a module-level
``__getattr__``. That hook runs only for a name the package does not already
hold, so any binding of the name in the package's own namespace wins every later
read and the owning submodule's value becomes unreachable through the package.
Two things create such a binding: a ``__getattr__`` that memoises the value it
resolved, and an ordinary ``setattr`` on the package.

Either one breaks restore-by-reassign, which is how ``monkeypatch`` puts a value
back: it reads the attribute to remember it, then assigns the remembered value
back. A harness that patches the owner first and the package second reads its
package baseline through the already patched owner, remembers the patched value,
and installs it in the package for the life of the process -- where the next test
in the same worker reads it as its own baseline.

These tests measure that, name by name, in both orderings: COLD, where the
harness's own read is the first read of the name, and WARM, where something has
read it already. A memoising ``__getattr__`` leaks only COLD, which is why the
orderings are separate cases rather than one.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

import pytest

#: Every package whose public surface resolves through a module-level ``__getattr__``.
PACKAGES = (
    "kiro_crew.config",
    "kiro_crew.dashboard",
    "kiro_crew.diag",
    "kiro_crew.mcp_gateway",
    "kiro_crew.stt",
)


def owners(package: str) -> dict[str, tuple[str, str]]:
    """Return ``{attribute: (owner module, symbol on that owner)}`` for *package*.

    Read from each package's own table, so a name added there is covered here
    without editing this file.
    """
    module = importlib.import_module(package)
    if package == "kiro_crew.stt":
        return {n: (f"{package}.{owner}", n) for n, owner in module._EXPORTS.items()}
    if package == "kiro_crew.config":
        return {n: ("kiro_crew.config.loader", n) for n in module.__all__}
    if package == "kiro_crew.diag":
        return dict(module._LAZY)
    if package in ("kiro_crew.dashboard", "kiro_crew.mcp_gateway"):
        return {n: (f"{package}.{o}", s) for n, (o, s) in module._LAZY.items()}
    raise AssertionError(f"{package} is listed in PACKAGES with no owner table rule")


#: One case per re-exported name, so a leak names the package and the name.
NAMES = [(package, name) for package in PACKAGES for name in sorted(owners(package))]


def _resolved(package: str, name: str) -> tuple[ModuleType, ModuleType, str]:
    module = importlib.import_module(package)
    owner_name, symbol = owners(package)[name]
    return module, importlib.import_module(owner_name), symbol


@pytest.mark.parametrize("package", PACKAGES)
def test_every_package_forwards_writes_through_a_module_subclass(package: str) -> None:
    """The package object is a ``ModuleType`` subclass, which is what carries the rule."""
    module = importlib.import_module(package)
    assert isinstance(module, ModuleType)
    assert type(module) is not ModuleType, (
        f"{package} is a plain module, so a setattr on it binds the name in the "
        "package instead of reaching the submodule that owns it"
    )


@pytest.mark.parametrize(("package", "name"), NAMES)
def test_reading_a_name_does_not_bind_it_in_the_package(package: str, name: str) -> None:
    """A read resolves the owner and caches the import, never the value."""
    module, _owner, _symbol = _resolved(package, name)
    getattr(module, name)
    assert name not in vars(module), (
        f"reading {package}.{name} bound it in the package namespace, which shadows "
        "__getattr__ for every later read"
    )


@pytest.mark.parametrize(("package", "name"), NAMES)
def test_undo_restores_the_owner_value_when_the_package_is_read_cold(
    package: str, name: str
) -> None:
    """Owner patched first, package second, with no package-level binding in the way.

    The precondition is part of the assertion: if any earlier read left the name
    bound in the package, the harness's read below resolves that binding instead
    of the owner and the ordering under test is not the cold one.
    """
    from _pytest.monkeypatch import MonkeyPatch

    module, owner, symbol = _resolved(package, name)
    assert name not in vars(module), (
        f"{package}.{name} is bound in the package namespace, so this read is not "
        "cold and the owner's value is already unreachable through the package"
    )
    original = getattr(owner, symbol)

    patch = MonkeyPatch()
    try:
        patch.setattr(owner, symbol, object())
        patch.setattr(module, name, object())
    finally:
        patch.undo()

    assert getattr(module, name) is original
    assert getattr(owner, symbol) is original


@pytest.mark.parametrize(("package", "name"), NAMES)
def test_undo_restores_the_owner_value_when_the_package_is_read_warm(
    package: str, name: str
) -> None:
    """Same ordering, after the name has already been read through the package."""
    from _pytest.monkeypatch import MonkeyPatch

    module, owner, symbol = _resolved(package, name)
    original = getattr(owner, symbol)
    getattr(module, name)

    patch = MonkeyPatch()
    try:
        patch.setattr(owner, symbol, object())
        patch.setattr(module, name, object())
    finally:
        patch.undo()

    assert getattr(module, name) is original
    assert getattr(owner, symbol) is original


@pytest.mark.parametrize(("package", "name"), NAMES)
def test_a_write_through_the_package_reaches_the_owner(
    package: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One value: the owner holds what was written through the package."""
    module, owner, symbol = _resolved(package, name)
    replacement = object()

    monkeypatch.setattr(module, name, replacement)

    assert getattr(owner, symbol) is replacement
    assert getattr(module, name) is replacement
    assert name not in vars(module)


@pytest.mark.parametrize(("package", "name"), NAMES)
def test_a_delete_through_the_package_reaches_the_owner(
    package: str, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete removes the one value, rather than a package-level shadow of it."""
    module, owner, symbol = _resolved(package, name)

    monkeypatch.delattr(module, name)

    assert not hasattr(owner, symbol)
    with pytest.raises(AttributeError):
        getattr(module, name)


@pytest.mark.parametrize("package", PACKAGES)
def test_an_unknown_attribute_still_raises_attribute_error(package: str) -> None:
    module = importlib.import_module(package)
    with pytest.raises(AttributeError):
        module.no_such_name_exists_here  # noqa: B018 - the access IS the assertion


@pytest.mark.parametrize("package", PACKAGES)
def test_no_re_exported_name_also_names_a_submodule(package: str) -> None:
    """The precondition ``lazy_exports.bind`` documents.

    The import system binds a submodule onto its parent with ``setattr``, which a
    forwarding package would send to the owner instead of binding the submodule.
    """
    module = importlib.import_module(package)
    submodules = {info.name for info in pkgutil.iter_modules(module.__path__)}
    assert submodules & set(owners(package)) == set()


@pytest.mark.parametrize("package", PACKAGES)
def test_every_public_name_resolves_through_the_package(package: str) -> None:
    """``__all__`` stays the surface, and every name on it is reachable."""
    module = importlib.import_module(package)
    for name in module.__all__:
        assert hasattr(module, name), f"{package}.__all__ names {name}, which does not resolve"


@pytest.mark.parametrize("package", PACKAGES)
def test_a_submodule_still_imports_through_the_package(package: str) -> None:
    """``from <package> import <submodule>`` keeps working past the ``__getattr__``."""
    module = importlib.import_module(package)
    leaf = sorted(info.name for info in pkgutil.iter_modules(module.__path__))[0]
    assert importlib.import_module(f"{package}.{leaf}") is getattr(module, leaf)


# ── the helper's own contract, on a synthetic package ──────────────────────────


@pytest.fixture()
def synthetic(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    """A package and an owner module wired through ``lazy_exports.bind``."""
    import sys

    from kiro_crew import lazy_exports

    owner = ModuleType("synthetic_owner")
    owner.real_name = "owned"  # type: ignore[attr-defined]
    package = ModuleType("synthetic_package")
    monkeypatch.setitem(sys.modules, "synthetic_owner", owner)
    monkeypatch.setitem(sys.modules, "synthetic_package", package)

    table = {"exported": ("synthetic_owner", "real_name")}
    package.__getattr__ = lazy_exports.bind("synthetic_package", table)  # type: ignore[attr-defined]
    return package, owner


def test_bind_reads_the_owner_symbol_under_its_package_name(synthetic) -> None:
    """The attribute and the owner's symbol are allowed to differ."""
    package, owner = synthetic
    assert package.exported == "owned"
    owner.real_name = "changed"
    assert package.exported == "changed"


def test_bind_sends_a_write_to_the_owner_symbol(synthetic) -> None:
    package, owner = synthetic
    package.exported = "written"
    assert owner.real_name == "written"
    assert "exported" not in vars(package)


def test_bind_sends_a_delete_to_the_owner_symbol(synthetic) -> None:
    package, owner = synthetic
    del package.exported
    assert not hasattr(owner, "real_name")


def test_bind_leaves_a_name_outside_the_table_on_the_package(synthetic) -> None:
    package, owner = synthetic
    package.unrelated = "local"
    assert vars(package)["unrelated"] == "local"
    assert not hasattr(owner, "unrelated")
    del package.unrelated
    assert "unrelated" not in vars(package)


def test_bind_raises_attribute_error_for_a_name_outside_the_table(synthetic) -> None:
    package, _owner = synthetic
    with pytest.raises(AttributeError, match="synthetic_package"):
        package.absent  # noqa: B018 - the access IS the assertion


def test_bind_imports_an_owner_only_when_a_name_is_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bind`` itself imports nothing, which is what keeps a cold path cold."""
    import sys

    from kiro_crew import lazy_exports

    package = ModuleType("synthetic_lazy_package")
    monkeypatch.setitem(sys.modules, "synthetic_lazy_package", package)
    imported: list[str] = []

    def record(name: str) -> ModuleType:
        imported.append(name)
        owner = ModuleType(name)
        owner.value = 7  # type: ignore[attr-defined]
        sys.modules[name] = owner
        return owner

    monkeypatch.setattr(lazy_exports.importlib, "import_module", record)
    package.__getattr__ = lazy_exports.bind(  # type: ignore[attr-defined]
        "synthetic_lazy_package", {"value": ("synthetic_never_imported", "value")}
    )
    assert imported == []

    assert package.value == 7
    assert imported == ["synthetic_never_imported"]

    assert package.value == 7
    assert imported == ["synthetic_never_imported"], "the import is cached, not repeated"
