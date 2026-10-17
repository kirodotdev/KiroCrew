"""A synthetic mirrored family for ``test_facade_mirror*.py``: two owners and a facade.

The family is a package ``facade_mirror_case_<test>`` written to ``tmp_path`` -- a facade
in the shape ``kiro_crew._facade_mirror`` documents over two owner modules, ``low`` and
``high`` -- loaded under names unique to the test and registered in :data:`sys.modules`
through ``monkeypatch``, so teardown removes every module it added. A package rather
than three top-level modules, because the owners of a real facade share its top-level
package and the mirrored-owner probe only accepts such an owner. ``second_facade`` adds a
legacy owner and a second facade with ``forwards`` and ``surface``.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from kiro_crew import _facade_mirror

_LOW = """
from __future__ import annotations

import json

LIMIT = 3
REGISTRY: dict[str, int] = {}
CLASH = "low"


def helper() -> str:
    return "real"


def low_reads_helper() -> str:
    return helper()


def low_reads_limit() -> int:
    return LIMIT


def low_dumps() -> str:
    return json.dumps({"low": 1})


def low_opens() -> object:
    return open
"""

_HIGH = """
from __future__ import annotations

import json

from {low} import LIMIT, REGISTRY, helper

CLASH = "high"


def high_reads_helper() -> str:
    return helper()


def high_reads_limit() -> int:
    return LIMIT


def high_dumps() -> str:
    return json.dumps({{"high": 1}})


def high_opens() -> object:
    return open
"""

_FACADE = '''
"""A synthetic facade in the documented shape; it also binds one name for its own code."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from {low} import helper

ONLY_HERE = "facade"


def facade_reads_helper() -> str:
    return helper()


_OWNERS = ("{low}", "{high}")
for _owner in _OWNERS:
    _importlib.import_module(_owner)
_EXPORTS: dict[str, str] = {{}}
_EXPORTS.update(
    _facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=("CLASH",)).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:
    from {low} import LIMIT, REGISTRY, low_dumps, low_opens, low_reads_helper  # noqa: F401
    from {low} import low_reads_limit  # noqa: F401
    from {high} import high_dumps, high_opens, high_reads_helper  # noqa: F401
    from {high} import high_reads_limit  # noqa: F401
'''


@dataclasses.dataclass
class Family:
    """One loaded synthetic family: the facade, its two owners, and their names."""

    facade: ModuleType
    low: ModuleType
    high: ModuleType
    sources: dict[str, str]
    monkeypatch: pytest.MonkeyPatch

    @property
    def index(self) -> _facade_mirror.MirrorIndex:
        return self.facade.__dict__[_facade_mirror.INDEX_ATTRIBUTE]

    @property
    def package_dir(self) -> Path:
        assert self.facade.__file__ is not None
        return Path(self.facade.__file__).parent

    def readers(self) -> tuple[str, str, str]:
        """What every reader of ``helper`` sees right now."""
        return (
            self.low.low_reads_helper(),
            self.high.high_reads_helper(),
            self.facade.facade_reads_helper(),
        )

    def reload_low(self) -> ModuleType:
        """Purge the low owner and import it again from the same file."""
        self.monkeypatch.delitem(sys.modules, self.low.__name__)
        return _load(self.low.__name__, self.package_dir / "low.py", self.monkeypatch)


def _load(
    name: str, path: Path, monkeypatch: pytest.MonkeyPatch, *, package: bool = False
) -> ModuleType:
    """Import *path* as module *name*, registered in sys.modules until teardown."""
    locations = [str(path.parent)] if package else None
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=locations)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    parent, _, leaf = name.rpartition(".")
    if parent:
        # What the import system does at the end of a load, so dotted patch targets
        # resolve through the package as they do for a real family.
        monkeypatch.setattr(sys.modules[parent], leaf, module, raising=False)
    return module


def build_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Family:
    """A package ``facade_mirror_case_<test>`` holding ``low``, ``high`` and ``facade``.

    A package rather than three top-level modules, because the owners of a real facade
    share its top-level package and the mirrored-owner probe only accepts such an owner.
    """
    package = "facade_mirror_case_" + re.sub(r"\W", "_", request.node.name)
    low, high, facade = f"{package}.low", f"{package}.high", f"{package}.facade"
    package_dir = tmp_path / package
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text('"""Synthetic family."""\n', encoding="utf-8")
    _load(package, package_dir / "__init__.py", monkeypatch, package=True)
    sources = {
        low: _LOW,
        high: _HIGH.format(low=low),
        facade: _FACADE.format(low=low, high=high),
    }
    modules = {}
    for name, text in sources.items():
        path = package_dir / f"{name.rpartition('.')[2]}.py"
        path.write_text(text, encoding="utf-8")
        modules[name] = _load(name, path, monkeypatch)
    return Family(
        facade=modules[facade],
        low=modules[low],
        high=modules[high],
        sources=sources,
        monkeypatch=monkeypatch,
    )


def _owners_binding(family: Family, name: str) -> list[str]:
    return [m.__name__ for m in (family.low, family.high) if name in vars(m)]


def _fake() -> str:
    return "fake"


_LEGACY = """
LEGACY = 1


def legacy_reads() -> int:
    return LEGACY
"""

_SECOND_FACADE = """
import importlib as _importlib
import sys as _sys

from kiro_crew import _facade_mirror

_OWNERS = ("{low}", "{high}")
for _owner in _OWNERS:
    _importlib.import_module(_owner)
_importlib.import_module("{legacy}")
_EXPORTS: dict[str, str] = {{}}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__],
        _OWNERS,
        excluded=("CLASH",),
        forwards={{"LEGACY": "{legacy}", "legacy_reads": "{legacy}"}},
        surface={surface},
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
"""


def second_facade(family: Family, surface: object = None) -> tuple[ModuleType, ModuleType]:
    """A legacy owner and a second facade over the family, with forwards and a surface."""
    package = family.facade.__name__.rpartition(".")[0]
    legacy_path = family.package_dir / "legacy.py"
    legacy_path.write_text(_LEGACY, encoding="utf-8")
    legacy = _load(f"{package}.legacy", legacy_path, family.monkeypatch)
    source = _SECOND_FACADE.format(
        low=family.low.__name__,
        high=family.high.__name__,
        legacy=legacy.__name__,
        surface=repr(surface),
    )
    path = family.package_dir / "second.py"
    path.write_text(source, encoding="utf-8")
    return _load(f"{package}.second", path, family.monkeypatch), legacy
