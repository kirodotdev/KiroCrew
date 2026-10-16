"""``dashboard/session_control.py`` is a mirrored facade over ``session_control_parts``.

The code lives in the owner modules under ``kiro_crew.dashboard.session_control_parts``;
``kiro_crew.dashboard.session_control`` stays the import path and the patch seam, built on
``kiro_crew._facade_mirror``. The contract every mirrored facade holds to (one object per
name, the ``TYPE_CHECKING`` manifest, no owner importing the facade, every patched name
reaching every reader) is ``test_facade_mirror_guards.py``'s. These cases pin what is
particular to this family: it is one of those facades, its owners are exactly the modules
of the package in layer order, no name of it had to be excluded, every owner logs under
the module's original logger name, and a moved class reports the owner it lives in.
"""

from __future__ import annotations

import ast
import importlib
import logging
import pkgutil
from pathlib import Path
from types import ModuleType

from kiro_crew import _facade_mirror, security_posture
from kiro_crew.dashboard import session_control, session_control_parts

_FACADE = session_control.__name__
_PACKAGE = session_control_parts.__name__
_MIRROR: _facade_mirror.MirrorIndex = vars(session_control)[_facade_mirror.INDEX_ATTRIBUTE]


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in _MIRROR.owners]


def _tree(module: ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text("utf-8"))


def _defined_names(owner: ModuleType) -> set[str]:
    return _facade_mirror._defined_names(_tree(owner))


def _in_family(module: str) -> bool:
    return module == _FACADE or module == _PACKAGE or module.startswith(f"{_PACKAGE}.")


def test_the_facade_serves_a_mirror_over_its_owner_package() -> None:
    assert type(session_control) is _facade_mirror.MirroredModule
    assert _MIRROR.facade == _FACADE
    assert dict(session_control._EXPORTS) == _MIRROR.served
    assert {"create_session", "stop_target"} <= set(_MIRROR.exports), "moved verbs are served"


def test_the_owners_are_the_package_modules_lowest_layer_first() -> None:
    package_modules = {
        f"{_PACKAGE}.{info.name}" for info in pkgutil.iter_modules(session_control_parts.__path__)
    }
    assert set(_MIRROR.owners) == package_modules
    assert _MIRROR.owners[0] == f"{_PACKAGE}.caller_access", "the identity layer sits lowest"


def test_no_name_of_the_family_is_excluded_from_the_mirror() -> None:
    """The family came from one namespace, so no name can mean two things in it."""
    assert _MIRROR.excluded == ()
    assert _MIRROR.divergent() == ()


def test_every_owner_logs_under_the_module_s_original_logger_name() -> None:
    """Moved records keep their logger, so log filters and captures keyed on it still match."""
    loggers = [vars(owner)["logger"] for owner in _owners() if "logger" in vars(owner)]
    assert loggers, "no owner logs, so this case measures nothing"
    assert {log.name for log in loggers} == {_FACADE}
    assert all(log is logging.getLogger(_FACADE) for log in loggers)


def test_a_moved_class_reports_the_owner_it_lives_in() -> None:
    classes = [
        (owner, value)
        for owner in _owners()
        for name, value in vars(owner).items()
        if isinstance(value, type) and name in _defined_names(owner)
    ]
    assert classes, "no owner defines a class, so this case measures nothing"
    for owner, cls in classes:
        assert cls.__module__ == owner.__name__
        assert getattr(session_control, cls.__name__) is cls


def _reexport_group() -> set[str]:
    """Names the facade's last ``TYPE_CHECKING`` block imports from outside the family."""
    blocks = [
        node
        for node in _tree(session_control).body
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
    assert {"fork_slot", "_FORK_DIRECTION_HEAD", "redact_and_truncate"} & imported_from_elsewhere
    assert len(imported_from_elsewhere) >= 50, "the scan found too few names to measure"
    assert _reexport_group() == imported_from_elsewhere


def test_the_drop_notice_is_its_own_output_path_beside_the_session_control_read() -> None:
    """The split moves the queued-message drop notice into its own module, so the
    Security panel counts it as its own output path (one more than before)."""
    rows = {
        (label, module)
        for label, module, _detail in security_posture._REDACTION_SINKS
        if "session_control" in module
    }
    assert rows == {
        (
            "Queued-message drop notice",
            f"dashboard/{_PACKAGE.rpartition('.')[2]}/delivery_containment.py",
        ),
        ("Session control read", f"dashboard/{_PACKAGE.rpartition('.')[2]}/session_status.py"),
    }


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
