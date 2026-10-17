"""One storage location per re-exported name in ``kiro_crew.security``.

``kiro_crew.security`` is a facade: the controls live in submodules of the
package and roughly forty modules import them through the package. A re-exported
name therefore has an owner, and the rule these tests hold is that the name's
value lives in that owner's namespace and nowhere else, and that the owner itself
is looked up in :data:`sys.modules` rather than remembered.

The cases are generated from the package's own ``_EXPORTS`` table, so the subject
is the CONDITION -- every name the facade re-exports -- and not the handful of
sites a change happened to touch. Re-introducing a second storage location
anywhere, for any name, fails here.

Two failure modes are why this is a test rather than a convention. A stale value
makes a patching test pass while exercising an object nobody runs. A stale owner
sends a write to a module ``sys.modules`` has already replaced. Both surface later,
in another file, under some shard splits and not others, with nothing pointing
back at the cause.

DUPLICATE, ON PURPOSE. ``test/test_lazy_reexport_storage.py`` holds the same rule
for the six lazily re-exporting packages, and this file holds it for
``kiro_crew.security`` alone so the two land in either order without touching one
another.

    Retire this file when ``test/test_lazy_reexport_storage.py`` on main contains a
    ``kiro_crew.security`` entry in ``PACKAGES``.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

import kiro_crew.security as facade
from kiro_crew.security import _exports

PACKAGE = "kiro_crew.security"

#: Every re-exported name, read from the package's own table so a name added
#: there is covered here without editing this file.
NAMES: tuple[str, ...] = tuple(sorted(facade._EXPORTS))

#: One representative per owner, enough for the per-owner cases without running
#: the whole table through them.
OWNER_MODULES: tuple[str, ...] = tuple(sorted(set(facade._EXPORTS.values())))


def _owner(name: str) -> ModuleType:
    return importlib.import_module(f"{PACKAGE}.{facade._EXPORTS[name]}")


@contextlib.contextmanager
def _owner_restored(module_name: str) -> Iterator[None]:
    """Put back BOTH places a purged-and-reimported owner leaves changed.

    A test that purges an owner and lets it be imported again has changed two
    things, not one. The obvious one is :data:`sys.modules`. The other is this
    package's own attribute: ``importlib`` finishes a submodule import by setting
    it on the parent package, so ``kiro_crew.security.paths`` is rebound to the
    fresh copy -- and that name is an ``_EXPORTS`` VALUE, not a key, so the write
    lands on the package itself rather than being forwarded.

    Restoring only ``sys.modules`` therefore leaves the package pointing at a
    module no reader runs, and every later ``monkeypatch.setattr(security.paths,
    ...)`` in the worker patches that dead copy. Both are captured here and put
    back together.
    """
    leaf = module_name.rsplit(".", 1)[1]
    had_module = module_name in sys.modules
    module = sys.modules.get(module_name)
    had_attribute = hasattr(facade, leaf)
    attribute = getattr(facade, leaf, None)

    def _restore() -> None:
        if had_module:
            sys.modules[module_name] = module  # type: ignore[assignment]
        else:
            sys.modules.pop(module_name, None)
        if had_attribute:
            setattr(facade, leaf, attribute)
        else:
            with contextlib.suppress(AttributeError):
                delattr(facade, leaf)

    try:
        yield
    except BaseException:
        _restore()
        raise
    _restore()
    # Proved, not assumed, and only on the path where the body itself succeeded, so
    # this can never stand in for a real failure. A case that purges and leaves the
    # package bound to a discarded copy is the very defect this file is about, so
    # every purge here demonstrates its own teardown rather than trusting it.
    assert getattr(facade, leaf, None) is sys.modules.get(module_name), (
        f"teardown left {PACKAGE}.{leaf} pointing at a module sys.modules does not "
        "hold, so every later patch through that attribute would miss every reader"
    )


def _source() -> str:
    return inspect.getsource(sys.modules[PACKAGE])


class TestTheTableIsTheSubject:
    """A truncated or derived table would make every case below vacuous."""

    def test_the_table_is_not_empty(self) -> None:
        assert len(NAMES) > 400

    def test_every_owner_is_a_submodule_of_this_package(self) -> None:
        for module in OWNER_MODULES:
            owner = importlib.import_module(f"{PACKAGE}.{module}")
            assert isinstance(owner, ModuleType)
            assert owner.__name__ == f"{PACKAGE}.{module}"

    def test_every_table_name_is_in_the_frozen_manifest(self) -> None:
        unrecorded = sorted(set(NAMES) - set(_exports.EXPORTED_NAMES))
        assert unrecorded == [], (
            "these names are re-exported but the frozen manifest does not list "
            f"them: {unrecorded}"
        )

    def test_the_table_is_declared_literally(self) -> None:
        """Derived from the owners, the table would agree with any facade.

        A literal table is what makes a name leaving its owner a failure here
        rather than a silent re-attribution.
        """
        tree = ast.parse(_source())
        declarations = [
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_EXPORTS"
        ]
        assert len(declarations) == 1
        assert isinstance(declarations[0].value, ast.Dict)


class TestOneStorageForTheValue:
    """The value lives in the owner's namespace, and the facade holds no copy."""

    @pytest.mark.parametrize("name", NAMES)
    def test_the_facade_binds_no_re_exported_name(self, name: str) -> None:
        assert name not in vars(facade), (
            f"{PACKAGE}.{name} is bound in the facade's own namespace, which is a "
            "second place its value lives and shadows the owner for every later read"
        )

    @pytest.mark.parametrize("name", NAMES)
    def test_reading_a_name_does_not_bind_it(self, name: str) -> None:
        getattr(facade, name)
        assert name not in vars(facade), (
            f"reading {PACKAGE}.{name} bound it in the facade, which shadows the "
            "resolver for every later read"
        )

    @pytest.mark.parametrize("name", NAMES)
    def test_a_read_answers_with_the_owners_object(self, name: str) -> None:
        owner = _owner(name)
        assert getattr(facade, name) is getattr(owner, name)

    @pytest.mark.parametrize("name", NAMES)
    def test_an_owner_write_is_seen_through_the_facade(self, name: str) -> None:
        """The direction a copy cannot follow.

        ``patch.object(<submodule>, ...)`` writes the owner. A facade holding its
        own copy keeps answering with the pre-patch object, so a reader that went
        through the facade runs the control the test believes it replaced.
        """
        owner = _owner(name)
        original = getattr(owner, name)
        sentinel = object()
        setattr(owner, name, sentinel)
        try:
            assert getattr(facade, name) is sentinel
        finally:
            setattr(owner, name, original)
        assert getattr(facade, name) is original

    @pytest.mark.parametrize("name", NAMES)
    def test_a_facade_write_reaches_the_owner(self, name: str) -> None:
        """The direction a patch fixture depends on.

        Code inside the owner resolves the name through its own globals, so a
        write that stopped at the facade would leave that code running the
        unpatched object.
        """
        owner = _owner(name)
        original = getattr(owner, name)
        sentinel = object()
        setattr(facade, name, sentinel)
        try:
            assert getattr(owner, name) is sentinel
            assert getattr(facade, name) is sentinel
        finally:
            setattr(facade, name, original)
        assert getattr(owner, name) is original

    @pytest.mark.parametrize("name", NAMES)
    def test_a_facade_delete_reaches_the_owner(self, name: str) -> None:
        owner = _owner(name)
        original = getattr(owner, name)
        try:
            delattr(facade, name)
            assert not hasattr(owner, name)
            assert not hasattr(facade, name)
        finally:
            setattr(facade, name, original)
        assert getattr(owner, name) is original
        assert getattr(facade, name) is original

    def test_the_facade_forwards_writes_through_a_module_subclass(self) -> None:
        assert isinstance(facade, ModuleType)
        assert type(facade) is not ModuleType, (
            "the facade is a plain module, so a setattr on it binds the name in the "
            "facade instead of reaching the owner"
        )

    def test_a_name_outside_the_table_stays_on_the_facade(self) -> None:
        """The forwarding is not a broadcast: an ordinary attribute is unaffected."""
        sentinel = "_security_single_storage_probe"
        assert sentinel not in facade._EXPORTS
        setattr(facade, sentinel, "local")
        try:
            assert vars(facade)[sentinel] == "local"
            for module in OWNER_MODULES:
                owner = importlib.import_module(f"{PACKAGE}.{module}")
                assert not hasattr(owner, sentinel)
        finally:
            delattr(facade, sentinel)
        assert sentinel not in vars(facade)


class TestOneStorageForTheOwner:
    """The owner is looked up in ``sys.modules``, never held in the facade."""

    def test_the_resolver_asks_the_import_system(self) -> None:
        source = _source()
        for fragment in (
            "def _owner(name: str) -> ModuleType:",
            "return sys.modules[module_name]",
            "return importlib.import_module(module_name)",
            "class _ReExportModule(ModuleType):",
            "def __setattr__(self, name: str, value: Any) -> None:",
            "def __delattr__(self, name: str) -> None:",
            "sys.modules[__name__].__class__ = _ReExportModule",
        ):
            assert fragment in source, f"the facade is missing {fragment!r}"

    def test_a_resolved_owner_is_read_without_calling_import_module(self) -> None:
        """The store answers; the import only fills it.

        ``importlib.import_module`` is an attribute of a module any caller can
        rebind, and tests do -- ``patch("importlib.import_module")`` appears three
        times in this repository for unrelated reasons. If a resolved owner were
        fetched by calling it, every read of every gate here would be answered by
        whatever that patch returns for as long as it is installed, which for a
        security predicate is a value chosen by unrelated code.

        Asserted by behaviour, not by reading the source: with the import refused
        outright, a name whose owner is already in ``sys.modules`` still reads.
        """
        name = "is_sensitive_path"
        expected = getattr(_owner(name), name)
        real = importlib.import_module

        def _refuse(target: str, package: str | None = None) -> ModuleType:
            raise AssertionError(f"resolution called import_module for {target!r}")

        importlib.import_module = _refuse  # type: ignore[assignment]
        try:
            assert getattr(facade, name) is expected
        finally:
            importlib.import_module = real  # type: ignore[assignment]

    def test_the_facade_keeps_no_resolved_owner_mapping(self) -> None:
        """A mapping of resolved owner MODULES is the second storage this removes."""
        source = _source()
        for forbidden in (
            "_EXPORT_OWNERS",
            "_OWNERS: dict",
            "_OWNERS.get(",
            "_OWNERS[",
            "globals()[name]",
        ):
            assert forbidden not in source, (
                f"the facade resolves or memoises its owner outside the import "
                f"system: {forbidden!r}"
            )

    def test_no_resolved_module_object_is_stored_in_the_table(self) -> None:
        for name, module in facade._EXPORTS.items():
            assert isinstance(module, str), (
                f"{name} maps to a module OBJECT, a second place that module is "
                "stored beside sys.modules"
            )

    def test_a_replaced_owner_is_seen_at_once(self) -> None:
        """A stand-in installed in ``sys.modules`` answers the next read."""
        name = "is_sensitive_path"
        module_name = f"{PACKAGE}.{facade._EXPORTS[name]}"
        real = sys.modules[module_name]
        stand_in = ModuleType(module_name)
        sentinel = object()
        setattr(stand_in, name, sentinel)
        sys.modules[module_name] = stand_in
        try:
            assert getattr(facade, name) is sentinel
        finally:
            sys.modules[module_name] = real
        assert getattr(facade, name) is getattr(real, name)

    def test_a_purged_and_reimported_owner_is_seen_at_once(self) -> None:
        """The genuine purge, not a stand-in: a fresh module object, freshly built."""
        name = "is_sensitive_path"
        module_name = f"{PACKAGE}.{facade._EXPORTS[name]}"
        with _owner_restored(module_name):
            stale = sys.modules[module_name]
            del sys.modules[module_name]
            fresh = importlib.import_module(module_name)
            assert fresh is not stale
            assert getattr(facade, name) is getattr(fresh, name)
            assert getattr(facade, name) is not getattr(stale, name)

            # The write half resolves the same module the read half answered with,
            # which is what keeps a monkeypatch round trip symmetric across a purge.
            original = getattr(fresh, name)
            sentinel = object()
            setattr(facade, name, sentinel)
            try:
                assert getattr(fresh, name) is sentinel
                assert getattr(stale, name) is not sentinel
            finally:
                setattr(facade, name, original)
            assert getattr(fresh, name) is original

    def test_a_purged_owner_is_seen_by_a_write_too(self) -> None:
        """A write must not land on a module ``sys.modules`` has replaced."""
        name = "DENIED_ROOT_PARTS"
        module_name = f"{PACKAGE}.{facade._EXPORTS[name]}"
        with _owner_restored(module_name):
            stale = sys.modules[module_name]
            del sys.modules[module_name]
            fresh = importlib.import_module(module_name)
            stale_before = getattr(stale, name)
            original = getattr(fresh, name)
            sentinel = ("probe",)
            setattr(facade, name, sentinel)
            try:
                assert getattr(fresh, name) is sentinel
                assert getattr(stale, name) is stale_before
            finally:
                setattr(facade, name, original)

    def test_a_purge_leaves_this_package_pointing_at_the_live_module(self) -> None:
        """The leak the restore above exists to stop, asserted directly.

        A submodule import ends by setting the submodule on its parent package, so
        a purge-and-reimport rebinds ``security.<leaf>``. If a test restored only
        :data:`sys.modules`, this package would keep the dead copy and every later
        patch through ``security.<leaf>`` would miss every reader.
        """
        module_name = f"{PACKAGE}.paths"
        with _owner_restored(module_name):
            del sys.modules[module_name]
            importlib.import_module(module_name)
        assert facade.paths is sys.modules[module_name]

    def test_no_submodule_binding_disagrees_with_the_store(self) -> None:
        """Read every binding back, not just the one a purge case touched.

        The package binds each submodule as an attribute so callers can patch at the
        owner. Each of those is a second place that module is named, and the only
        thing keeping it honest is that nothing rebinds it to a copy. This reads all
        of them against :data:`sys.modules`, so a leak left anywhere -- by a case
        here, or by any other test sharing this worker -- fails rather than waiting
        to surface as a patch that reaches nobody.
        """
        disagreeing = []
        for leaf in sorted({module for module in facade._EXPORTS.values()}):
            bound = getattr(facade, leaf, None)
            stored = sys.modules.get(f"{PACKAGE}.{leaf}")
            if bound is not stored:
                disagreeing.append(leaf)
        assert not disagreeing, (
            f"{PACKAGE} binds submodule(s) that are not the object sys.modules holds, "
            f"so a patch through them reaches no reader: {disagreeing}"
        )


class TestTheFacadesOwnCodeReadsTheOwner:
    """A function defined in the facade cannot reach ``__getattr__``.

    Module ``__getattr__`` answers an attribute access from OUTSIDE. A function
    defined in the facade resolves a bare global through the facade's own
    namespace, which the resolver never sees, so such a reference would need the
    facade to bind the name -- the second storage this removes. Those functions
    ask for the owner and read the name off it instead.

    Enumerated from the source by condition: any Load of a table name at any
    nesting depth. A single reintroduced bare reference fails here, which is also
    what would fail at runtime with a ``NameError`` on a path a test may not cover.
    """

    @staticmethod
    def _bare_loads() -> list[tuple[int, str]]:
        tree = ast.parse(_source())
        import_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        return [
            (node.lineno, node.id)
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in facade._EXPORTS
            and node.lineno not in import_lines
        ]

    def test_the_facade_reads_no_re_exported_name_as_a_bare_global(self) -> None:
        left = self._bare_loads()
        assert left == [], (
            "the facade reads these re-exported names from its own namespace, which "
            f"only resolves if it binds them: {left}"
        )

    def test_the_enumeration_can_fail(self) -> None:
        """The condition above is discriminating, not vacuously empty.

        A name in the table, loaded as a bare global, is what the scan looks for.
        Assert the scan finds one in a source that has one, so an empty result on
        the real source means absence rather than a scan that matches nothing.
        """
        sample = next(iter(NAMES))
        tree = ast.parse(f"def f():\n    return {sample}\n")
        found = [
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in facade._EXPORTS
        ]
        assert found == [sample]


class TestAnUnresolvableGateFailsClosed:
    """A read can now raise where it could not before, and that must deny.

    Resolving the owner through the import system introduces a failure mode the
    eager binding did not have: the import can fail. On a security predicate the
    hazard is not the exception, it is an exception QUIETLY becoming a falsy
    answer -- ``getattr(security, "is_sensitive_path", None)`` swallows
    ``AttributeError``, and a ``None`` a caller reads as "not sensitive" grants
    exactly what the gate exists to refuse.

    So the facade raises ``ImportError`` for a table name whose owner will not
    import, and ``AttributeError`` only for a name that does not exist. The
    repository's own guards are built on that distinction: a caller wraps its
    import in ``except ImportError`` and fails closed from a flag.
    """

    GATES = (
        "is_sensitive_path",
        "is_sensitive_write_path",
        "is_sensitive_resolved_path",
        "is_sensitive_canonical_path",
        "path_contains_sensitive",
    )

    @pytest.mark.parametrize("name", GATES)
    def test_the_gate_is_in_the_table(self, name: str) -> None:
        assert name in facade._EXPORTS

    @staticmethod
    @contextlib.contextmanager
    def _owner_will_not_resolve(module_name: str) -> Iterator[None]:
        """Make one owner genuinely unresolvable, both halves of resolution.

        The resolver reads :data:`sys.modules` and imports only on a miss, so an
        owner that cannot be obtained is absent from the store AND refuses to
        import -- which is also the real shape of the condition: a module that
        fails to import never reaches ``sys.modules``. Refusing the import alone
        would leave the store answering and prove nothing.
        """
        real = importlib.import_module

        def _refuse(target: str, package: str | None = None) -> ModuleType:
            if target == module_name:
                raise ImportError(f"refused for the test: {target}")
            return real(target, package)

        with _owner_restored(module_name):
            del sys.modules[module_name]
            importlib.import_module = _refuse  # type: ignore[assignment]
            try:
                yield
            finally:
                importlib.import_module = real  # type: ignore[assignment]

    @pytest.mark.parametrize("name", GATES)
    def test_an_unimportable_owner_raises_rather_than_answering(self, name: str) -> None:
        module_name = f"{PACKAGE}.{facade._EXPORTS[name]}"
        with self._owner_will_not_resolve(module_name), pytest.raises(ImportError):
            getattr(facade, name)

    @pytest.mark.parametrize("name", GATES)
    def test_a_defaulted_getattr_cannot_manufacture_a_falsy_gate(self, name: str) -> None:
        """``getattr(facade, gate, None)`` must not hand back ``None``.

        This is the fail-open shape: the default swallows ``AttributeError``, so a
        resolver that reported an unimportable owner that way would answer ``None``
        and a caller testing it for truth would read "not sensitive".
        """
        module_name = f"{PACKAGE}.{facade._EXPORTS[name]}"
        with self._owner_will_not_resolve(module_name), pytest.raises(ImportError):
            getattr(facade, name, None)

    def test_a_name_that_does_not_exist_still_raises_attribute_error(self) -> None:
        """The other half of the distinction, so the rule above stays usable."""
        with pytest.raises(AttributeError):
            facade._no_name_of_this_shape_is_exported  # noqa: B018
        assert getattr(facade, "_no_name_of_this_shape_is_exported", "absent") == "absent"

    def test_the_repository_still_fails_closed_without_the_security_module(self) -> None:
        """The guard the ImportError is for: a reader that cannot import it denies.

        ``spec_builder``'s parser is the in-tree example -- with no security module
        it treats every path as sensitive rather than waving them through. Pinned
        on the SHAPE of that guard, not on its wording: the handler has to catch
        the class an import failure raises, and the fallback gate has to answer
        ``True``. An edit that narrowed either fails here.
        """
        parsers = Path(
            importlib.import_module("kiro_crew.apps.builtins.spec_builder.backend.parsers").__file__
            or ""
        )
        tree = ast.parse(parsers.read_text(encoding="utf-8"))

        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and any(
                isinstance(stmt, ast.ImportFrom) and stmt.module == PACKAGE
                for stmt in ast.walk(node)
            )
        ]
        assert guarded, f"the parser imports {PACKAGE} without a guard"

        def _catches_import_failure(handler: ast.ExceptHandler) -> bool:
            names: list[str] = []
            if isinstance(handler.type, ast.Name):
                names = [handler.type.id]
            elif isinstance(handler.type, ast.Tuple):
                names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
            return bool({"ImportError", "Exception", "BaseException"} & set(names))

        handlers = [h for node in guarded for h in node.handlers]
        assert any(_catches_import_failure(h) for h in handlers), (
            "no handler catches the class an unresolvable security import raises, so "
            "the failure would propagate past the fail-closed fallback"
        )

        fallbacks = [
            node
            for handler in handlers
            for node in ast.walk(handler)
            if isinstance(node, ast.FunctionDef) and node.name == "is_sensitive_path"
        ]
        assert fallbacks, "the guard installs no fail-closed is_sensitive_path fallback"
        returns = [
            node.value.value
            for fallback in fallbacks
            for node in ast.walk(fallback)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, bool)
        ]
        assert returns and set(returns) == {True}, (
            "the no-security fallback no longer answers True for every path, so an "
            f"unresolvable gate would stop denying: {returns}"
        )


class TestTheStarImportSurface:
    """``import *`` consults ``__all__`` and never reaches ``__getattr__``.

    That is the one read path resolution cannot serve on its own: the star form is
    resolved by the import machinery against a declared list, so a package that
    binds nothing and declares nothing carries only what it happens to hold -- and
    a caller's first use of a re-exported predicate is a ``NameError``. The list
    is therefore derived from the table, which is what keeps it from becoming a
    second place a name has to be written down.

    Asserted on the declared list itself rather than by running a star import: what
    the import statement does with ``__all__`` is the language's, and the contents
    of ``__all__`` are this module's.
    """

    def test_every_public_table_name_is_declared(self) -> None:
        public = {name for name in facade._EXPORTS if not name.startswith("_")}
        missing = sorted(public - set(facade.__all__))
        assert not missing, (
            f"{len(missing)} re-exported name(s) are not declared, so a star importer "
            f"gets a NameError at first use: {missing[:8]}"
        )

    def test_no_private_table_name_is_declared(self) -> None:
        """A private name is re-exported for a direct read, never by ``import *``."""
        private = sorted(name for name in facade.__all__ if name.startswith("_"))
        assert not private, f"the declared list carries private name(s): {private[:8]}"

    def test_every_declared_name_resolves(self) -> None:
        """A star import reads each declared name, so one that cannot resolve raises."""
        unresolvable = []
        for name in facade.__all__:
            try:
                getattr(facade, name)
            except (AttributeError, ImportError) as exc:
                unresolvable.append(f"{name}: {type(exc).__name__}")
        assert not unresolvable, (
            "declared name(s) do not resolve, so a star import would raise rather "
            f"than bind: {unresolvable[:8]}"
        )

    def test_the_list_is_derived_from_the_table(self) -> None:
        """A written-out list would be a second place every name has to appear."""
        tree = ast.parse(_source())
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
        ]
        assert len(assignments) == 1, "the facade declares no single __all__"
        value = assignments[0].value
        assert not isinstance(value, (ast.List, ast.Tuple, ast.Set)), (
            "__all__ is written out as a literal, which is a second place every "
            "exported name has to be kept in step with _EXPORTS"
        )
        read_names = {node.id for node in ast.walk(value) if isinstance(node, ast.Name)}
        assert (
            "_EXPORTS" in read_names
        ), f"__all__ is not derived from the export table: it reads {sorted(read_names)}"
