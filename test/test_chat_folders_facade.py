"""Contracts the ``dashboard.chat_folders`` facade keeps over its ``chat_folders_*`` owners.

``test_facade_mirror_guards.py`` holds every mirrored facade to the generic contract
(one object per name, the TYPE_CHECKING manifest, no owner importing its facade, patch
reach). This file pins what is particular to this one: its owner list and layering, the
names its kept routes read from an owner, the module name its exceptions and its owners'
log records carry, and its frozen import inventory, which stands in for the F401 check
``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.dashboard import chat_folders

FACADE = "kiro_crew.dashboard.chat_folders"
_OWNERS = tuple(
    f"kiro_crew.dashboard.chat_folders_{name}" for name in ("icons", "paths", "records", "routes")
)

#: Every name the facade's import statements bound before its owners moved out. They
#: stay bound here because callers and tests read and patch them as attributes of it.
_BASE_IMPORTS = frozenset("""
    Any DashboardState FOLDER_CREATE KNOWN_INTERNAL_CALLERS LoopBoundLock
    MEMBER_CHAT_PRINCIPAL_KEY Path _coerce_requested_mode _folder_owner_app _is_descendant
    _subtree_holds_foreign_folder allow_create annotations app_owns_transcript asyncio
    audit_app_slot_denial crosses_memory_silo deny_app_slot_access effective_request_app
    effective_session_key folder_principal is_channel_session_key is_sensitive_path
    is_unc_shape load_manual_order_after_listen logging memory_silo_fence os pinned_fs
    pinned_session_order read_bounded_json redact_credentials redact_exfiltration_urls
    refuse_unattributable_caller request_origin run_bg_oneliner save_slot_off_loop sel
    slot_history_key slot_not_found subprocess_executor tags_write_lock time unc_probe_allowed
    unicodedata uuid validate_file_path validate_folder_tag_ids voice_runtime_workspace_conflict
    weakref web
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its class swap.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})

#: What the routes the facade keeps read from an owner, bound here by import.
_KEPT_ROUTE_READS = {
    "kiro_crew.dashboard.chat_folders_records": {
        "_agent_in_channel_reachable_session",
        "_audit_origin",
        "_unhide_folder",
        "member_slot_write_refused",
        "note_folder_filed",
    }
}

_EXCEPTIONS = (
    "FolderCreateError",
    "FolderOwnershipError",
    "FolderNameExistsError",
    "FolderCapError",
)


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(chat_folders)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _tree(module_name: str) -> ast.Module:
    path = Path(importlib.import_module(module_name).__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


def _defined_by(module_name: str) -> set[str]:
    return _facade_mirror._defined_names(_tree(module_name))


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def read_names(*sources: str) -> set[str]:
    """Every name the family's code reads: a bare load, or the base of an attribute read."""
    found: set[str] = set()
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                found.add(node.id)
    return found


def unexpected_imports(
    source: str, owner_names: dict[str, set[str]], family_reads: set[str]
) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, and from each owner module only
    names that owner defines. An import beyond them is accounted for when the family's
    code reads the name, so a later change can import what it uses here. Imports under
    ``if TYPE_CHECKING:`` bind nothing at run time and are the mirror guard's business,
    so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if _is_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | family_reads:
                found.append(name)
    return found


def _family_sources() -> list[str]:
    paths = [Path(importlib.import_module(m).__file__) for m in (FACADE, *_OWNERS)]
    return [path.read_text(encoding="utf-8") for path in paths]


def _owner_names() -> dict[str, set[str]]:
    return {owner: _defined_by(owner) for owner in _OWNERS}


def test_owners_are_every_chat_folders_sibling_lowest_layer_first() -> None:
    index = _index()
    assert (index.owners, index.excluded, dict(index.forwards)) == (_OWNERS, (), {})
    assert index.divergent() == ()
    siblings = sorted(
        f"kiro_crew.dashboard.{path.stem}"
        for path in Path(chat_folders.__file__).parent.glob("chat_folders_*.py")
    )
    assert siblings == sorted(_OWNERS)


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    for position, owner in enumerate(_OWNERS):
        imported = {
            node.module
            for node in ast.walk(_tree(owner))
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(FACADE)
        }
        assert imported <= set(_OWNERS[:position]), owner


def test_the_kept_routes_bind_what_they_read_from_an_owner() -> None:
    """The slot routes kept here read these owner names as their own globals, so they are
    bound here by import, the same object as the owner's; every other owner-defined name
    is served from its owner."""
    bound = {
        node.module: {alias.asname or alias.name for alias in node.names}
        for node in _tree(FACADE).body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }
    assert bound == _KEPT_ROUTE_READS
    for owner, names in bound.items():
        module = importlib.import_module(owner)
        assert all(vars(chat_folders)[name] is vars(module)[name] for name in names)
    defined = {name for owner in _OWNERS for name in _defined_by(owner)}
    bound_here = defined & set(vars(chat_folders))
    # ``logger`` is the facade's own: each owner binds the same named logger.
    assert bound_here == {name for names in bound.values() for name in names} | {"logger"}
    served = defined - bound_here
    assert served and served <= set(_index().exports)
    assert all(getattr(chat_folders, name) is not None for name in served)


def test_a_moved_exception_keeps_the_facade_module_name() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``."""
    for name in _EXCEPTIONS:
        cls = getattr(chat_folders, name)
        assert issubclass(cls, Exception)
        assert (cls.__module__, cls.__qualname__) == (FACADE, name)


def test_every_owner_logs_under_the_facade_logger_name() -> None:
    """Log capture and filters key on ``kiro_crew.dashboard.chat_folders``."""
    logged = 0
    for owner in _OWNERS:
        for call in ast.walk(_tree(owner)):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "getLogger"
            ):
                assert [ast.literal_eval(arg) for arg in call.args] == [FACADE], owner
        module = importlib.import_module(owner)
        if "logger" in vars(module):
            assert module.logger is chat_folders.logger is logging.getLogger(FACADE)
            logged += 1
    assert logged >= 3


def test_a_patch_through_the_facade_reaches_the_owner_that_reads_it() -> None:
    routes = importlib.import_module("kiro_crew.dashboard.chat_folders_routes")
    icons = importlib.import_module("kiro_crew.dashboard.chat_folders_icons")
    before_save, before_emoji = routes.save_slot_off_loop, icons.generate_emoji_for_name
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chat_folders, "save_slot_off_loop", "patched-save")
        patch.setattr(chat_folders, "generate_emoji_for_name", "patched-emoji")
        assert routes.save_slot_off_loop == "patched-save"
        assert icons.generate_emoji_for_name == "patched-emoji"
    assert routes.save_slot_off_loop is before_save
    assert icons.generate_emoji_for_name is before_emoji


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    source = Path(chat_folders.__file__).read_text(encoding="utf-8")
    family_reads = read_names(*_family_sources())
    assert unexpected_imports(source, _owner_names(), family_reads) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported, while an import the family's code
    reads is accounted for."""
    source = Path(chat_folders.__file__).read_text(encoding="utf-8")
    planted = source + (
        "\nimport shutil\nimport fnmatch\n"
        "from kiro_crew.dashboard.chat_folders_icons import create_folder_record\n"
    )
    family_reads = read_names(*_family_sources(), "_uses = fnmatch.fnmatch\n")
    assert unexpected_imports(planted, _owner_names(), family_reads) == [
        "shutil",
        "kiro_crew.dashboard.chat_folders_icons.create_folder_record",
    ]
