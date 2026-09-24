"""The crew_log package's lazy re-export seam -- one storage location per name.

``kiro_crew.crew_log`` resolves its public names from the submodules that define
them (:pep:`562`). The rule these tests pin is that the submodule is the ONLY
place such a value lives: a read through the package reads the submodule, and a
write through the package writes the submodule.

That rule is what makes a patch on one of these names undoable. The
restore-by-reassign protocol a test harness uses -- ``monkeypatch`` reads the
attribute to remember it, then assigns the remembered value back -- is only
correct when there is one value to remember. With a second copy bound in the
package's own namespace, the harness's baseline read resolves whatever the
submodule holds at that moment, so patching the submodule first makes it remember
the PATCHED value and its restore installs that value for the life of the process.

The laziness itself is pinned too, because the seam exists to keep ``store``,
``schema`` and ``lease`` off the flag-off gateway boot path.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew.crew_log as pkg
from kiro_crew.crew_log import _EXPORTS

#: A module-level integer, so a wrong restore is a wrong number rather than a
#: type error, and the owner is the module the clamp arithmetic reads.
NAME = "MAX_REF_SPAN"
OWNER = "schema"


def _owner_of(name: str):
    """Import and return the submodule that defines ``name``."""
    return importlib.import_module(f"kiro_crew.crew_log.{_EXPORTS[name]}")


@pytest.fixture(autouse=True)
def _no_shadow_left_behind():
    """Strip any package-level copy of a re-exported name a test created.

    Popped out of ``__dict__`` directly: ``delattr`` on the package forwards to
    the owner, which would delete the real attribute instead of a stray copy.
    """
    yield
    for name in _EXPORTS:
        pkg.__dict__.pop(name, None)


def test_a_read_through_the_package_leaves_no_copy_on_the_package():
    """Resolving a name must not bind it in the package's own namespace.

    A bound copy wins every later read, because ``__getattr__`` runs only for a
    name the package does not already hold.
    """
    for name in _EXPORTS:
        getattr(pkg, name)
    assert [name for name in _EXPORTS if name in pkg.__dict__] == []


def test_a_write_through_the_package_reaches_the_owning_submodule():
    """``setattr`` on the package is a write to the submodule that owns the name."""
    schema = _owner_of(NAME)
    real = schema.MAX_REF_SPAN
    try:
        setattr(pkg, NAME, 7)
        assert schema.MAX_REF_SPAN == 7
        assert getattr(pkg, NAME) == 7
    finally:
        setattr(schema, NAME, real)


def test_a_write_to_the_owner_is_visible_through_the_package(monkeypatch):
    """The package reports the owner's CURRENT value, not the one it first saw."""
    schema = _owner_of(NAME)
    assert getattr(pkg, NAME) == schema.MAX_REF_SPAN
    monkeypatch.setattr(schema, NAME, 3)
    assert getattr(pkg, NAME) == 3


def test_patching_the_owner_then_the_package_is_undone():
    """The order that makes a harness read its baseline through a patched owner.

    This is the whole point of the seam. The harness patches the submodule, then
    patches the package; its baseline read for the package therefore resolves the
    patched value. Teardown must still leave the real value in place.
    """
    schema = _owner_of(NAME)
    real = schema.MAX_REF_SPAN
    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(schema, NAME, 2)
        inner.setattr(pkg, NAME, 2)
        assert getattr(pkg, NAME) == 2
    assert schema.MAX_REF_SPAN == real
    assert getattr(pkg, NAME) == real


def test_patching_the_package_then_the_owner_is_undone():
    """The other order is undone too, so no test has to know the safe one."""
    schema = _owner_of(NAME)
    real = schema.MAX_REF_SPAN
    with pytest.MonkeyPatch.context() as inner:
        inner.setattr(pkg, NAME, 2)
        inner.setattr(schema, NAME, 2)
        assert getattr(pkg, NAME) == 2
    assert schema.MAX_REF_SPAN == real
    assert getattr(pkg, NAME) == real


def test_every_re_exported_name_survives_a_patch_undo_cycle():
    """The hazard is a property of the seam, so it is pinned over every name.

    Identity comparison, not equality: the sentinel is a bare object, so a name
    whose real value happens to compare equal to it cannot mask a leak.
    """
    leaked = []
    for name in sorted(_EXPORTS):
        owner = _owner_of(name)
        real = getattr(owner, name)
        sentinel = object()
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(owner, name, sentinel)
            inner.setattr(pkg, name, sentinel)
        if getattr(pkg, name) is not real or getattr(owner, name) is not real:
            leaked.append(name)
    assert leaked == []


def test_deleting_a_re_exported_name_through_the_package_reaches_the_owner():
    """``delattr`` follows the same single-location rule as ``setattr``."""
    schema = _owner_of(NAME)
    real = schema.MAX_REF_SPAN
    try:
        delattr(pkg, NAME)
        assert not hasattr(schema, NAME)
        with pytest.raises(AttributeError):
            getattr(pkg, NAME)
    finally:
        setattr(schema, NAME, real)


def test_no_submodule_name_is_also_a_re_exported_name():
    """A collision here would break importing that submodule at all.

    Importing ``kiro_crew.crew_log.store`` makes the import machinery bind
    ``store`` on the package with an ordinary ``setattr``. A name the seam
    forwards would send that binding into the submodule instead of the package,
    so the two namespaces must not overlap.
    """
    package = Path(pkg.__file__).parent
    submodules = {path.stem for path in package.glob("*.py")} - {"__init__"}
    assert sorted(submodules & set(_EXPORTS)) == []


def test_importing_a_submodule_still_binds_it_on_the_package():
    """The import machinery's own write to the package is not forwarded."""
    importlib.import_module("kiro_crew.crew_log.store")
    assert pkg.__dict__["store"].__name__ == "kiro_crew.crew_log.store"


def test_a_name_the_package_owns_itself_is_not_forwarded():
    """Only the re-exported names are forwarded; the package keeps its own.

    Without this the seam could forward every write and still pass the tests
    above, which would put private module state somewhere it does not belong.
    """
    try:
        pkg._probe_local_name = 11  # type: ignore[attr-defined]
        assert pkg.__dict__["_probe_local_name"] == 11
        del pkg._probe_local_name  # type: ignore[attr-defined]
        assert "_probe_local_name" not in pkg.__dict__
    finally:
        pkg.__dict__.pop("_probe_local_name", None)


def test_an_unknown_name_still_raises_attribute_error():
    """The seam answers a name it does not export the way a module does."""
    with pytest.raises(AttributeError, match="has no attribute 'not_exported'"):
        getattr(pkg, "not_exported")


def test_importing_the_package_does_not_import_its_storage_submodules():
    """The seam exists to keep storage off the flag-off boot path.

    A clean interpreter is the only place this is observable: this suite has
    already imported the storage submodules, so an in-process check would read
    its own imports rather than a plain package import.
    """
    probe = (
        "import importlib, json, sys;"
        "importlib.import_module('kiro_crew.crew_log');"
        "print(json.dumps(sorted(k for k in sys.modules "
        "if k.startswith('kiro_crew.crew_log.'))))"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [os.path.abspath(sys.executable), "-B", "-c", probe],
        # The full environment is inherited because Windows needs SYSTEMROOT and
        # friends to start the interpreter at all; ``-B`` keeps the child from
        # writing bytecode into the checkout.
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "KIROCREW_HOME": os.environ.get("KIROCREW_HOME", ""),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]) == []
