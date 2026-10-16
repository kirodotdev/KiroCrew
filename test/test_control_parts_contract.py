"""``mcp_tools/control.py`` is a mirrored facade over ``mcp_tools/control_parts``.

The descriptors, the session-directive tools and the monitoring-loop tools live in the
owner modules under ``kiro_crew.mcp_tools.control_parts``; ``kiro_crew.mcp_tools.control``
stays the domain module ``mcp_tools.DOMAIN_MODULES`` names, the import path and the patch
seam, built on ``kiro_crew._facade_mirror``. The contract every mirrored facade holds to
(one object per name, the ``TYPE_CHECKING`` manifest, no owner importing the facade,
every patched name reaching every reader) is ``test_facade_mirror_guards.py``'s. These
cases pin what is particular to this family: its owners are exactly the package's modules
in layer order, no name had to be excluded, both halves of every tool still resolve on
the facade, an owner that logs does so under the module's original logger name (also
when the facade runs as ``__main__``), and the names the
owners import from elsewhere stay visible to type checkers through the facade.
"""

from __future__ import annotations

import ast
import importlib
import logging
import os
import pkgutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.mcp_tools import DOMAIN_MODULES, control, control_parts
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = control.__name__
_PACKAGE = control_parts.__name__
_MIRROR: _facade_mirror.MirrorIndex = vars(control)[_facade_mirror.INDEX_ATTRIBUTE]


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in _MIRROR.owners]


def _tree(module: ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text("utf-8"))


def _in_family(module: str) -> bool:
    return module == _FACADE or module == _PACKAGE or module.startswith(f"{_PACKAGE}.")


def test_the_facade_serves_a_mirror_over_its_owner_package() -> None:
    assert type(control) is _facade_mirror.MirroredModule
    assert _MIRROR.facade == _FACADE
    assert dict(control._EXPORTS) == _MIRROR.served
    assert {"schemas", "_emit_directive"} <= set(_MIRROR.exports), "moved names are served"


def test_the_owners_are_the_package_modules_lowest_layer_first() -> None:
    package_modules = {
        f"{_PACKAGE}.{info.name}" for info in pkgutil.iter_modules(control_parts.__path__)
    }
    assert set(_MIRROR.owners) == package_modules
    assert _MIRROR.owners[-1] == f"{_PACKAGE}.monitor", "the monitoring tools sit highest"


def test_the_owner_package_is_not_a_tool_domain() -> None:
    """``DOMAIN_MODULES`` names the facade; an owner listed there would advertise twice."""
    assert "control" in DOMAIN_MODULES
    assert not any(domain.startswith("control_parts") for domain in DOMAIN_MODULES)
    halves = {"schemas", "HANDLERS"}
    assert not [
        name
        for name, value in vars(control_parts).items()
        if name in halves and not isinstance(value, ModuleType)
    ], "the package binds a tool half of its own"


def test_no_name_of_the_family_is_excluded_from_the_mirror() -> None:
    """The family came from one namespace, so no name can mean two things in it."""
    assert _MIRROR.excluded == ()
    assert _MIRROR.divergent() == ()


def test_both_halves_of_every_tool_resolve_on_the_facade() -> None:
    """``build_tool_list`` reads ``control.schemas`` and ``dispatch`` reads
    ``control.HANDLERS``; the descriptor builder is the owner's own function, so the
    ``names_only`` keyword the names-only build detects is still on it."""
    from kiro_crew.mcp_tools import _schemas_accepts_names_only

    assert control.schemas is vars(importlib.import_module(f"{_PACKAGE}.schemas"))["schemas"]
    assert _schemas_accepts_names_only(control.schemas)
    names = [tool["name"] for tool in control.schemas(names_only=True)]
    assert names and set(names) == set(control.HANDLERS)
    for handler in control.HANDLERS.values():
        assert getattr(control, handler.__name__) is handler


def test_every_owner_logs_under_the_module_s_original_logger_name() -> None:
    """Moved records keep their logger, so log filters and captures keyed on it still match."""
    loggers = [vars(owner)["logger"] for owner in _owners() if "logger" in vars(owner)]
    assert loggers, "no owner logs, so this case measures nothing"
    assert {log.name for log in loggers} == {_FACADE}
    assert all(log is logging.getLogger(_FACADE) for log in loggers)
    assert control.logger is logging.getLogger(_FACADE)


def test_the_facade_still_runs_as_main(tmp_path: Path) -> None:
    """``python -m kiro_crew.mcp_tools.control`` imported cleanly before the split. Run as
    ``__main__``, the facade's ``__name__`` is not its dotted name, so a logger bound from
    ``__name__`` would be a second object beside the owners' and the mirror would refuse
    the install; the facade binds it by name. Run in a child, from ``tmp_path``, so the
    import touches nothing of this process."""
    env = dict(os.environ)
    src = str(Path(kiro_crew.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", _FACADE],
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr


def _reexport_group() -> set[str]:
    """Names the facade's last ``TYPE_CHECKING`` block imports from outside the family."""
    blocks = [
        node
        for node in _tree(control).body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
    ]
    imports = [sub for sub in blocks[-1].body if isinstance(sub, ast.ImportFrom)]
    assert imports and not any(_in_family(sub.module or "") for sub in imports)
    return {alias.asname or alias.name for sub in imports for alias in sub.names}


def test_the_names_owners_import_from_elsewhere_stay_visible_to_type_checkers() -> None:
    """A served name an owner imports from another ``kiro_crew`` module was the facade's
    own import before the split; the last ``TYPE_CHECKING`` block keeps it there for
    mypy and IDEs, and names nothing else."""
    imported_from_elsewhere = set()
    for name, owner_name in _MIRROR.exports.items():
        owner = importlib.import_module(owner_name)
        for node in _tree(owner).body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiro_crew"):
                bound = {alias.asname or alias.name for alias in node.names}
                if name in bound and not _in_family(node.module or ""):
                    imported_from_elsewhere.add(name)
    assert {"runtime_ceiling_secs", "MONITOR_START_SCHEMA"} <= imported_from_elsewhere
    assert len(imported_from_elsewhere) >= 30, "the scan found too few names to measure"
    assert _reexport_group() == imported_from_elsewhere


def test_owners_import_only_owners_listed_before_them() -> None:
    """Lowest layer first: an owner reads a sibling only when the sibling comes earlier."""
    order = {name: position for position, name in enumerate(_MIRROR.owners)}
    edges = 0
    for owner_name in _MIRROR.owners:
        for node in ast.walk(_tree(importlib.import_module(owner_name))):
            if isinstance(node, ast.ImportFrom) and node.module in order:
                edges += 1
                assert order[node.module] < order[owner_name], (owner_name, node.module)
    assert edges > 0, "no owner imports a sibling, so this case measures nothing"


def test_a_patch_through_the_facade_reaches_the_moved_descriptor_read() -> None:
    """``runtime_ceiling_secs`` is read by the descriptor builder and the arming tools,
    both moved; a patch through the facade reaches both owners and is undone in both."""
    holders = [importlib.import_module(name) for name in _MIRROR.holders["runtime_ceiling_secs"]]
    assert {owner.__name__.rpartition(".")[2] for owner in holders} == {"schemas", "monitor"}
    original = control.runtime_ceiling_secs
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(control, "runtime_ceiling_secs", lambda: 3600)
        assert all(vars(owner)["runtime_ceiling_secs"]() == 3600 for owner in holders)
        monitor_start = next(t for t in control.schemas() if t["name"] == "monitor_start")
        assert "configured max 3600" in str(monitor_start)
    assert all(vars(owner)["runtime_ceiling_secs"] is original for owner in holders)
