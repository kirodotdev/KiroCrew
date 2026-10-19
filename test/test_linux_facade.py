"""``kiro_crew.service.linux`` stays the one import path and patch surface of its owners.

The systemd service module was split into sibling owners (``linux_<role>.py``) behind a
:mod:`kiro_crew._facade_mirror` facade. These tests pin what the split must keep:

* the facade serves a mirror over exactly the ``linux_*`` modules beside it, and every
  module-level name the unsplit file bound still resolves through it, to the object each
  owner holds;
* a patch through the facade reaches every module of the family that reads the name,
  and its undo restores each one;
* each owner imports only the owners below it, and nothing outside the family imports
  an owner: callers install, uninstall, query and restart the service through the facade;
* every owner logs as ``kiro_crew.service.linux``, so its records keep the facade's
  dotted name;
* every name the facade serves from outside the family is named in a ``TYPE_CHECKING``
  group, so ``from kiro_crew.service.linux import <name>`` stays visible to type checkers;
* ``ServiceInstallError`` keeps the facade's ``__module__``: it is an exception, and its
  dotted name is what a traceback shows.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import importlib
import logging
from collections.abc import Iterable
from pathlib import Path
from unittest import mock

import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.service import linux as facade

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_linux_facade")

_SRC = Path(kiro_crew.__file__).resolve().parent
_FACADE = "kiro_crew.service.linux"
_OWNER_PREFIX = "kiro_crew.service.linux_"

#: Every module-level name ``service/linux.py`` bound before the split.
_BASE_MODULE_NAMES = frozenset("""
    AliasHolder ENV_DIR ENV_FILE_PATH LIVE_HOLDER_EXIT_CODE MANAGED_MARKER_LINE OwnedUnit
    Path RESTART_NOT_UP RESTART_REFUSED RESTART_UNCONFIRMED RestartReport SERVICE_NAME
    ScopeRestart ServiceInstallError TOTAL_SHUTDOWN_BUDGET_SECS UNIT_PATH USER_UNIT_SUBDIR
    UTF8_TEXT UninstallReport _ENV_FILE_TEMPLATE _RESTART_POLL_SECS _RESTART_SETTLE_SECS
    _SHOW_PROPERTIES _STOPPED_STATES _SYSTEMD_BOOTED_DIR _ScopeTeardown _UP_STATES
    _UnitState _confirm_up _current_group _current_uid _current_user
    _env_file_is_untouched_seed _finish_scope _first_line _home_for_user
    _install_file_via_sudo _installer_unit_path _is_mask_of_our_name _journal_hint
    _linger_enabled _loaded_restart_prevent _not_ours _owned_unit _privilege_prefix
    _privilege_refusal _remove_owned _remove_stale_owned _require_privilege _restart_scope
    _running_scopes _scope_finished _sd_quote _seed_env_file _stale_unit_line
    _stop_and_disable _sudo_capture _sudo_run _sudo_run_checked _systemctl _teardown_owned
    _teardown_refusal _teardown_scope _unit_state _user_scope_remedy
    _user_scope_unreachable_reason _write_unit_via_sudo alias_holder apparmor dataclass
    install install_apparmor_profile install_launcher_profile is_active is_up kirocrew_bin
    linger_warning_for_service_account log logging os remove_apparmor_profile
    remove_launcher_profile render_unit restart selinux selinux_refusal
    selinux_start_failure_hint service_environment shlex shutil status stop subprocess sys
    system_restart_command_hint systemctl_user_env systemd_quote tempfile time
    trusted_system_bin uninstall unit_file_carries_managed_marker user_restart_command_hint
    user_unit_active user_unit_file_path user_unit_installed user_unit_path
    """.split())

#: Names the service suites patch through the facade, each read by more than one module
#: of the family (``time`` only by the facade's own restart confirmation).
_PATCHED_THROUGH_THE_FACADE = (
    "UNIT_PATH",
    "ENV_FILE_PATH",
    "_current_user",
    "_home_for_user",
    "_privilege_prefix",
    "_require_privilege",
    "_sudo_run",
    "_systemctl",
    "_unit_state",
    "kirocrew_bin",
    "time",
)


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> tuple[str, ...]:
    return (_FACADE, *_index().owners)


def _source(module_name: str) -> str:
    path = importlib.import_module(module_name).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def test_the_facade_serves_a_mirror_over_every_sibling_owner() -> None:
    assert isinstance(facade, _facade_mirror.MirroredModule)
    index = _index()
    on_disk = sorted(
        f"kiro_crew.service.{path.stem}" for path in (_SRC / "service").glob("linux_*.py")
    )
    assert sorted(index.owners) == on_disk
    assert index.owners == facade._OWNERS
    assert index.excluded == ()
    assert index.divergent() == ()


def test_every_base_module_name_resolves_through_the_facade() -> None:
    index = _index()
    missing = sorted(name for name in _BASE_MODULE_NAMES if not hasattr(facade, name))
    assert missing == []
    for name in sorted(_BASE_MODULE_NAMES):
        for holder in index.holders.get(name, ()):
            assert getattr(importlib.import_module(holder), name) is getattr(facade, name), name


@pytest.mark.parametrize("name", _PATCHED_THROUGH_THE_FACADE)
def test_a_patch_through_the_facade_reaches_every_family_reader(name: str) -> None:
    modules = [importlib.import_module(member) for member in _family()]
    readers = [module for module in modules if name in vars(module)]
    original = {module.__name__: vars(module)[name] for module in readers}
    assert readers, f"no module of the family binds {name}"
    sentinel = mock.MagicMock(name=f"patched-{name}")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(facade, name, sentinel)
        assert {m.__name__ for m in readers if vars(m)[name] is not sentinel} == set()
    assert {m.__name__: vars(m)[name] for m in readers} == original


def test_the_patched_names_are_read_across_the_family() -> None:
    """The reach check above is only worth having for names more than one member reads."""
    modules = [importlib.import_module(member) for member in _family()]
    shared = {
        name
        for name in _PATCHED_THROUGH_THE_FACADE
        if sum(name in vars(module) for module in modules) > 1
    }
    assert shared == set(_PATCHED_THROUGH_THE_FACADE) - {"time"}
    assert [m.__name__ for m in modules if "time" in vars(m)] == [_FACADE]


def test_every_owner_logs_as_the_facade() -> None:
    shared = logging.getLogger(_FACADE)
    assert facade.log is shared
    loggers = {owner: vars(importlib.import_module(owner)).get("log") for owner in _index().owners}
    assert any(log is shared for log in loggers.values())
    assert {owner: log for owner, log in loggers.items() if log not in (None, shared)} == {}


def test_the_install_error_keeps_the_facade_module() -> None:
    error = facade.ServiceInstallError
    assert error.__module__ == _FACADE
    assert error.__qualname__ == "ServiceInstallError"
    assert getattr(importlib.import_module(error.__module__), error.__qualname__) is error


# --------------------------------------------------------------------------- #
# What the type checkers see
# --------------------------------------------------------------------------- #


def _served_imports(index: _facade_mirror.MirrorIndex) -> set[str]:
    defined = set().union(
        *(_facade_mirror._defined_names(ast.parse(_source(o))) for o in index.owners)
    )
    family = {*index.owners, _FACADE}
    return {
        name
        for name in index.exports
        if name not in defined
        and name != "TYPE_CHECKING"
        and getattr(getattr(facade, name), "__name__", None) not in family
    }


def _typing_names(source: str, owners: Iterable[str]) -> set[str]:
    """Names the ``TYPE_CHECKING`` groups of *source* import from outside *owners*."""
    named: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module not in owners:
                    named |= {alias.asname or alias.name for alias in sub.names}
                elif isinstance(sub, ast.Import):
                    named |= {alias.asname or alias.name.split(".")[0] for alias in sub.names}
    return named


def test_every_served_import_is_named_for_type_checkers() -> None:
    """A name the family imports and the facade serves is in a ``TYPE_CHECKING`` group.

    The facade binds none of them, so this group is what keeps
    ``from kiro_crew.service.linux import kirocrew_bin`` visible to mypy.
    """
    index = _index()
    served = _served_imports(index)
    named = _typing_names(_source(_FACADE), index.owners)
    assert {"kirocrew_bin", "subprocess"} <= served, "the census found nothing to check"
    assert sorted(served - named) == []
    assert sorted(named - served) == []


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    index = _index()
    source = _source(_FACADE)
    assert "kirocrew_bin" in _typing_names(source, index.owners)
    pruned = source.replace("        kirocrew_bin,\n", "", 1)
    assert pruned != source
    assert "kirocrew_bin" not in _typing_names(pruned, index.owners)


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(_OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    family = {f"service/{name.rpartition('.')[2]}.py" for name in _family()}
    sources = source_corpus.iter_candidate_sources(require_all=("linux_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "service/linux_uninstall.py" in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.service.linux_unit import render_unit\n",
        root / "other.py": "from kiro_crew.service import linux_systemctl as low\n",
        root / "service" / "relative.py": "from .linux_install import install\n",
        root / "service" / "deep" / "up.py": "from .. import linux_uninstall\n",
        root / "facade_user.py": "from kiro_crew.service.linux import install\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "service/relative.py",
        "service/deep/up.py",
    }


def _owner_imports(source: str, module: str) -> set[str]:
    """The family owners *source* (the text of *module*) imports, outside ``TYPE_CHECKING``."""
    tree = ast.parse(source)
    typing_only = {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }
    found = set()
    for node in ast.walk(tree):
        if id(node) in typing_only or not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _imported_modules(node, module):
            if name.startswith(_OWNER_PREFIX):
                found.add(".".join(name.split(".")[:3]))
    return found


def test_each_owner_imports_only_the_owners_before_it() -> None:
    owners = _index().owners
    for position, owner in enumerate(owners):
        imported = _owner_imports(_source(owner), owner)
        assert imported <= set(owners[:position]), (owner, sorted(imported))
    assert _owner_imports(_source(owners[-1]), owners[-1]), "the layer scan found nothing"


def test_the_layer_scan_sees_an_upward_import() -> None:
    planted = "from kiro_crew.service.linux_uninstall import uninstall\n"
    assert _owner_imports(planted, "kiro_crew.service.linux_unit") == {
        "kiro_crew.service.linux_uninstall"
    }
    guarded = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    " + planted
    assert _owner_imports(guarded, "kiro_crew.service.linux_unit") == set()
