"""A lazily re-exported name has one storage location, in every package that has one.

Six packages re-export their public surface through a module-level
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

The same one-storage rule applies to the OWNER as well as the value: each package
asks ``importlib`` for its owner on every read rather than holding a mapping of
its own, so a test that replaces or reimports an owner is seen through the
package, and a thread reading a name while another is still importing its owner
waits for that import instead of seeing a half-built module.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Every package whose public surface resolves through a module-level ``__getattr__``.
PACKAGES = (
    "kiro_crew.config",
    "kiro_crew.crew_log",
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
    if package in ("kiro_crew.crew_log", "kiro_crew.stt"):
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
    """The precondition each package's forwarding ``__setattr__`` rests on.

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


# ── one rule, spelled once per package ─────────────────────────────────────────
#
# The mechanism cannot live in a shared ``kiro_crew`` module: importing
# ``kiro_crew.config.paths`` must pull in no other ``kiro_crew`` submodule
# (``test_config_paths.TestLeafPurity``), and a shared helper would be one. So each
# package spells the rule itself, and these tests are the single place that holds
# the six spellings to one shape -- a package that reimplements it differently, or
# a seventh that copies half of it, fails here rather than in another file's
# unrelated test months later.


@pytest.mark.parametrize("package", PACKAGES)
def test_every_package_spells_the_rule_the_same_way(package: str) -> None:
    """Both halves of the rule are present, under the same names, in every package."""
    module = importlib.import_module(package)
    source = inspect.getsource(module)
    for fragment in (
        "def _owner(name: str) -> ModuleType:",
        "return importlib.import_module(module_name)",
        "class _ReExportModule(ModuleType):",
        "def __setattr__(self, name: str, value: Any) -> None:",
        "def __delattr__(self, name: str) -> None:",
        "sys.modules[__name__].__class__ = _ReExportModule",
    ):
        assert fragment in source, f"{package} is missing {fragment!r}"
    for forbidden in ("_OWNERS: dict", "_OWNERS.get(", "_OWNERS[", "sys.modules.get(module_name)"):
        assert forbidden not in source, f"{package} resolves its owner outside the import system"


@pytest.mark.parametrize("package", PACKAGES)
def test_no_package_caches_a_resolved_value_in_its_own_namespace(package: str) -> None:
    """The memoising half: nothing writes a resolved value back into ``globals()``."""
    source = inspect.getsource(importlib.import_module(package))
    assert "globals()[name]" not in source, f"{package} memoises a resolved value"


@pytest.mark.parametrize("package", PACKAGES)
def test_a_name_outside_the_table_stays_on_the_package(package: str) -> None:
    """Only re-exported names are forwarded; an ordinary attribute is unaffected."""
    module = importlib.import_module(package)
    sentinel = "_lazy_reexport_storage_probe"
    assert sentinel not in owners(package)
    setattr(module, sentinel, "local")
    try:
        assert vars(module)[sentinel] == "local"
    finally:
        delattr(module, sentinel)
    assert sentinel not in vars(module)


@pytest.mark.parametrize("package", PACKAGES)
def test_the_rule_imports_no_extra_kiro_crew_module(package: str) -> None:
    """Installing the rule costs no module import, which is why it is spelled inline.

    A shared helper module would appear in ``sys.modules`` here, and for
    ``kiro_crew.config`` that is exactly the leaf-purity regression
    ``test_config_paths.TestLeafPurity`` catches. Measured in a subprocess so the
    warm modules in this process cannot mask it.
    """
    code = (
        "import sys\n"
        f"import {package}\n"
        "print(','.join(sorted(m for m in sys.modules if m.startswith('kiro_crew'))))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=True, env=env, **UTF8_TEXT
    )
    loaded = {m for m in out.stdout.strip().split(",") if m}
    owner_modules = {owner for owner, _symbol in owners(package).values()}
    assert (
        loaded & owner_modules == set()
    ), f"importing {package} eagerly loaded its owners: {sorted(loaded & owner_modules)}"


@pytest.mark.parametrize("package", PACKAGES)
def test_every_read_resolves_the_owner_through_the_import_system(package: str) -> None:
    """Each read asks ``importlib`` for the owner, so nothing here can go stale.

    Asking every time is what makes the import system the single storage location:
    it answers from ``sys.modules`` and it waits on the import lock while an owner's
    body is still running, which a mapping held in the package can do neither of.
    """
    module = importlib.import_module(package)
    name = sorted(owners(package))[0]
    owner_name, symbol = owners(package)[name]

    getattr(module, name)  # import the owner once
    calls: list[str] = []
    real_import = importlib.import_module

    def counting(target: str, *args: object, **kwargs: object) -> ModuleType:
        calls.append(target)
        return real_import(target, *args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(module.importlib, "import_module", counting):
        first = getattr(module, name)
        second = getattr(module, name)
    assert calls == [owner_name, owner_name], f"{package} answered a read from its own state"

    owner = importlib.import_module(owner_name)
    assert first is second is getattr(owner, symbol)


@pytest.mark.parametrize("package", PACKAGES)
def test_the_package_follows_its_owner_to_a_new_module_object(package: str) -> None:
    """``sys.modules`` is the owner's one storage location, so a swap is seen at once.

    Purging a module and importing it again is an idiom this suite uses in twenty
    files. A package holding its own resolved-owner mapping answers from the
    module it resolved first while a direct importer answers from the one in
    ``sys.modules`` -- two storage locations for the owner, which is the same split
    this rule removes for the value. A stand-in module stands for the reimported
    one so no owner's module body is executed twice here.
    """
    module = importlib.import_module(package)
    name = sorted(owners(package))[0]
    owner_name, symbol = owners(package)[name]

    getattr(module, name)  # resolve the owner, so any private mapping is warm
    real_owner = sys.modules[owner_name]
    stand_in = ModuleType(owner_name)
    read_sentinel = object()
    setattr(stand_in, symbol, read_sentinel)
    try:
        sys.modules[owner_name] = stand_in
        assert getattr(module, name) is read_sentinel, f"{package} read the replaced owner"
        write_sentinel = object()
        setattr(module, name, write_sentinel)
        assert (
            getattr(stand_in, symbol) is write_sentinel
        ), f"a write through {package} missed the owner in sys.modules"
        assert getattr(real_owner, symbol) is not write_sentinel, "the write hit the old owner"
    finally:
        sys.modules[owner_name] = real_owner


def test_a_genuine_purge_and_reimport_is_seen_through_the_package() -> None:
    """The stand-in above is not the only path: a real reimport behaves the same.

    One owner carries this, because importing a module again runs its body again.

    Importing a submodule also binds it on its parent package, and ``recorder`` is
    not a re-exported name, so that binding lands in the package's own namespace
    rather than being forwarded. Teardown puts it back: ``from kiro_crew.diag
    import recorder`` resolves through that attribute, so leaving it on the
    discarded module would hand a later reader in the same worker a module whose
    ``Recorder`` is this test's sentinel.
    """
    diag = importlib.import_module("kiro_crew.diag")
    owner_name = "kiro_crew.diag.recorder"
    stale = importlib.import_module(owner_name)
    assert diag.Recorder is stale.Recorder  # resolve the owner before purging it
    had_parent_attr = "recorder" in vars(diag)
    parent_attr = vars(diag).get("recorder")
    try:
        del sys.modules[owner_name]
        fresh = importlib.import_module(owner_name)
        assert fresh is not stale, "the reimport handed back the same module object"
        sentinel = object()
        fresh.Recorder = sentinel
        assert diag.Recorder is sentinel, "the package read the purged module"
    finally:
        sys.modules[owner_name] = stale
        if had_parent_attr:
            diag.recorder = parent_attr
        elif "recorder" in vars(diag):
            del diag.recorder

    assert vars(diag).get("recorder") is parent_attr, "teardown left the discarded module bound"
    assert diag.Recorder is stale.Recorder, "teardown left the sentinel reachable"
