"""Guards for the ``kiro_crew.security`` facade.

The security controls are split by responsibility across submodules of the
``security`` package while ``kiro_crew.security`` stays the only import path.
Roughly forty modules import from it, the platform layer registers a seam site
on it by dotted name, and tests reach private helpers as attributes of it and
patch them by dotted string. "The split changed nothing for a caller" is
therefore a claim about two properties, and this module is what makes it a
tested claim rather than a remembered one:

* every name in the frozen manifest resolves on the facade, bound to the SAME
  object the submodule that owns it holds; and
* an attribute written on the facade reaches that owning submodule, because code
  inside the submodule resolves the name through its own globals and would
  otherwise keep running the unpatched object -- a patch that passes while
  testing nothing.

The failure both guards exist to catch is silent. A missing re-export surfaces
as an unrelated test's ``AttributeError`` several commits after the move that
dropped it, and an unmirrored patch surfaces as a test that no longer exercises
what its name says.
"""

from __future__ import annotations

from types import ModuleType

import pytest

import kiro_crew.security as facade
from kiro_crew.security import _exports


class TestExportManifest:
    def test_the_manifest_is_not_empty(self) -> None:
        """A truncated manifest makes every assertion below vacuously true."""
        assert len(_exports.EXPORTED_NAMES) > 400

    def test_the_manifest_has_no_duplicate_or_dunder_entries(self) -> None:
        names = _exports.EXPORTED_NAMES
        assert len(set(names)) == len(names)
        assert [n for n in names if n.startswith("__") and n.endswith("__")] == []

    def test_every_manifest_name_resolves_on_the_facade(self) -> None:
        missing = [name for name in _exports.EXPORTED_NAMES if not hasattr(facade, name)]
        assert missing == [], (
            "these names left the facade, so every caller and patch site reaching "
            f"them by attribute is broken: {missing}"
        )

    def test_a_facade_name_is_the_owning_submodules_object(self) -> None:
        """Re-export by identity, not by copy.

        A submodule's own callers resolve the name through its globals, so a
        facade holding a DIFFERENT object of the same name means two live
        versions of one control.
        """
        divergent = [
            name
            for name, owner in facade._EXPORT_OWNERS.items()
            if getattr(facade, name) is not getattr(owner, name)
        ]
        assert divergent == [], divergent

    def test_every_owned_name_is_in_the_manifest(self) -> None:
        """A submodule cannot introduce a name the manifest does not record."""
        unrecorded = sorted(set(facade._EXPORT_OWNERS) - set(_exports.EXPORTED_NAMES))
        assert unrecorded == [], (
            "a submodule owns these re-exported names but the frozen manifest does "
            f"not list them: {unrecorded}"
        )

    def test_owners_are_submodules_of_this_package(self) -> None:
        for name, owner in facade._EXPORT_OWNERS.items():
            assert isinstance(owner, ModuleType), name
            assert owner.__name__.startswith("kiro_crew.security."), name


class TestPatchMirroring:
    """A write on the facade has to land in the namespace the owner reads."""

    def test_the_facade_is_the_mirroring_module_type(self) -> None:
        assert type(facade).__name__ == "_MirroringModule"

    @staticmethod
    def _one_owned_name() -> tuple[str, ModuleType]:
        owners = facade._EXPORT_OWNERS
        if not owners:
            pytest.skip("no name has moved out of the facade yet")
        return next(iter(sorted(owners.items(), key=lambda kv: kv[0])))

    def test_setattr_reaches_the_owning_submodule(self) -> None:
        name, owner = self._one_owned_name()
        original = getattr(owner, name)
        sentinel = object()
        setattr(facade, name, sentinel)
        try:
            assert getattr(owner, name) is sentinel
            assert getattr(facade, name) is sentinel
        finally:
            setattr(facade, name, original)
        assert getattr(owner, name) is original

    def test_delattr_reaches_the_owning_submodule(self) -> None:
        name, owner = self._one_owned_name()
        original = getattr(owner, name)
        try:
            delattr(facade, name)
            assert not hasattr(owner, name)
            assert not hasattr(facade, name)
        finally:
            setattr(facade, name, original)
        assert getattr(owner, name) is original
        assert getattr(facade, name) is original

    def test_a_name_no_submodule_owns_is_set_on_the_facade_only(self) -> None:
        """The mirror is not a broadcast: an unowned name stays local."""
        unowned = "_facade_probe_name_not_owned_by_any_submodule"
        assert unowned not in facade._EXPORT_OWNERS
        setattr(facade, unowned, 1)
        try:
            assert getattr(facade, unowned) == 1
            for submodule in facade._SUBMODULES:
                assert not hasattr(submodule, unowned)
        finally:
            delattr(facade, unowned)
