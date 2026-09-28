"""The AWS Control backup engine's composition: one facade over private owners.

``backend.backup`` is the engine's only import path and patch surface, and its
responsibilities live in ``backend.backup_parts``. Three things have to stay true for
that split to be invisible to every caller, and each is pinned here in the direction
that would catch a regression rather than in the direction that restates the code.

* Every name of the engine's surface resolves on the facade, to the object its owner
  holds (:data:`FROZEN_NAMES`, frozen rather than derived, because a list derived from
  the facade agrees with any facade).
* A name and the symbol it denotes cannot come apart: every module that holds a name
  holds the same object, and a write through the facade reaches all of them -- the
  one-namespace behaviour ``monkeypatch.setattr(backup, ...)`` relies on across
  several hundred patch sites.
* The owners form one acyclic stack under the facade, none of them importing it,
  and the constructs other gates pin to ``backup.py`` by path stay there.
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import inspect
import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from unittest import mock

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import (
    catalog,
    egress_text,
    fingerprints,
    identity,
    layer_b,
    ledger,
    nightly,
    retention,
    state,
    traversal,
    uploads,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "src/kiro_crew/apps/builtins/aws_control/backend"
_PARTS_DIR = _BACKEND / "backup_parts"
_PARTS_PACKAGE = "kiro_crew.apps.builtins.aws_control.backend.backup_parts"

#: The owners in the facade's declared layer order, lowest first.
PARTS: tuple[ModuleType, ...] = (
    egress_text,
    state,
    identity,
    fingerprints,
    traversal,
    ledger,
    layer_b,
    nightly,
    uploads,
    catalog,
    retention,
)

#: The engine's module-level surface: every name ``backup`` answers for, dunders
#: excluded, as the one-module engine bound them. A name leaves this list only when the
#: symbol it names is deliberately deleted.
FROZEN_NAMES: tuple[str, ...] = (
    "APP_NAME",
    "AWSError",
    "Any",
    "BLOCK_HOST_UNSUPPORTED",
    "BLOCK_OTHER_ACCOUNT",
    "BLOCK_REDACTION_ON",
    "CALLER_OWNER",
    "CALLER_SCHEDULED",
    "Callable",
    "FAILURE_ERROR_MAX_CHARS",
    "INSTALL_KEY",
    "IO",
    "JOB_KINDS",
    "KEY_SEP",
    "KIND_SESSIONS",
    "KIND_SNAPSHOT",
    "KIND_SUBPATHS",
    "LABEL_MAX_CHARS",
    "LABEL_OBJECT_NAME",
    "MAX_OTHER_INSTALLS",
    "MAX_RECORDED_VERSIONS",
    "MAX_REMEMBERED_UPLOADS",
    "NIGHTLY_FAILURE_STATE_KEY",
    "NIGHTLY_RETRY_BACKOFF_SECS",
    "NIGHTLY_WINDOW_SECS",
    "NamedTuple",
    "NoReturn",
    "ORIGIN_LEGACY",
    "ORIGIN_OTHER",
    "ORIGIN_SELF",
    "ORIGIN_UNVERIFIED",
    "Optional",
    "Path",
    "RETENTION_KEEP_MIN",
    "RETENTION_KEEP_STATE_KEY",
    "RETENTION_UNCLAIMED_STATE_KEY",
    "RETENTION_UNRECORDED_STATE_KEY",
    "SEL_OP_BASELINE_PROBE",
    "SEL_OP_RETENTION",
    "SEL_OP_UPLOAD",
    "SESSIONS_CONVERSATIONS_RETAINED_KEY",
    "SESSIONS_DIR_NAME",
    "SESSIONS_LAYER_B_KEY",
    "SESSIONS_LAYER_B_SCOPE_KEY",
    "SESSIONS_LAYER_B_SCOPE_WITH_CONVERSATIONS",
    "STAGING_NAME_MAX_BYTES",
    "STATE_DIR_LEAF",
    "UnprovenArchive",
    "_AUTHORIZE_TIMEOUT_SECS",
    "_CAN_PIN_TRAVERSAL",
    "_CONVERSATIONS_ARC_PREFIX",
    "_CONVERSATIONS_DB_ARCNAME",
    "_CONVERSATIONS_MANIFEST_ARCNAME",
    "_CONVERSATION_BATCH_ROWS",
    "_CONVERSATION_MAX_CELL_BYTES",
    "_CONVERSATION_READ_ID",
    "_CONVERSATION_TABLES",
    "_ConversationExport",
    "_ConversationTooLarge",
    "_INSTALL_ID_RE",
    "_KIND_BY_SUBPATH",
    "_MAX_TREE_DEPTH",
    "_NIGHTLY_CONSENT_READERS",
    "_NO_PINNING_REASON",
    "_O_DIRECTORY",
    "_O_NOFOLLOW",
    "_O_NONBLOCK",
    "_PUSH_TIMEOUT_SECS",
    "_RETENTION_GATE",
    "_RUN_CONVERSATIONS_RETAINED",
    "_RedactionFailed",
    "_RetentionAuthorizationWithdrawn",
    "_RetentionCountWithdrawn",
    "_SNAPSHOT_MANIFEST_NAME",
    "_STATE_LOCK_TIMEOUT_SECS",
    "_STOP",
    "_ScratchExportUnsafe",
    "_StateUnreadable",
    "_UNCONDITIONAL_RUN_WRITE",
    "_VOLATILE_MANIFEST_FIELDS",
    "_a_day_since_last_run",
    "_account_state",
    "_account_view",
    "_account_view_checked",
    "_add_bytes",
    "_add_open_file",
    "_add_pinned",
    "_add_tree",
    "_archive_entries",
    "_archive_row",
    "_archive_sort_key",
    "_audit_layer_b_decision",
    "_audit_layer_b_grant",
    "_audit_retention",
    "_audit_unfiled_authorization",
    "_authorize_recovery_read",
    "_authorize_upload",
    "_backoff_withholds",
    "_body_fingerprint",
    "_checked",
    "_clamp_retention_keep",
    "_clear_nightly_failure",
    "_conversation_scratch_parent",
    "_copy_table",
    "_current_version_is_ours",
    "_default_label",
    "_delete_under_the_retention_gate",
    "_export_cli_conversations",
    "_fallback_identity",
    "_fallback_lock",
    "_forget_unpersisted",
    "_granted",
    "_install_folders",
    "_is_provable_version_id",
    "_key_basename",
    "_key_segments",
    "_kiro_cli_conversation_db",
    "_locked_state_update",
    "_manifest_digest",
    "_merge_pending",
    "_merge_unpersisted",
    "_merge_uploads",
    "_newest_first",
    "_open_pinned_scratch",
    "_prune_recorded_versions",
    "_prune_remote_archives",
    "_publish_label",
    "_read_state_checked",
    "_read_state_for_update",
    "_record_run",
    "_record_run_locked",
    "_record_skip",
    "_record_unclaimed",
    "_record_unrecorded",
    "_recover_recorded_version",
    "_redact_egress",
    "_redacted_row",
    "_refuse_an_oversized_cell",
    "_refuse_upload",
    "_release_persisted_versions",
    "_remember_unpersisted",
    "_retention_keep_for_sweep",
    "_run_is_newer",
    "_run_lock",
    "_run_process",
    "_run_sequence",
    "_set_conversations_retained",
    "_staging_name",
    "_stamp",
    "_state_key",
    "_state_lock",
    "_state_path",
    "_store_relocated_outside_the_fence",
    "_store_write_time",
    "_stored_identity",
    "_tree_fingerprint",
    "_unattended_sessions_redaction_gap",
    "_unchanged_baseline",
    "_unpersisted_lock",
    "_unpersisted_runs",
    "_unpersisted_uploads",
    "_unpersisted_versions",
    "_upload_lock",
    "_uploaded_objects_locked",
    "a_retained_archive_carries_conversations",
    "accounts_mod",
    "annotations",
    "app_data_dir",
    "atomic_write",
    "classify_key",
    "clear_stop",
    "contextlib",
    "data_home",
    "dt",
    "due_for_nightly",
    "due_for_sessions_nightly",
    "errno",
    "file_lock",
    "first_linked_ancestor",
    "hashlib",
    "hooks",
    "install_identity",
    "io",
    "is_link_or_junction",
    "json",
    "kind_unavailable_reason",
    "kiro_sessions_dir",
    "last_runs",
    "layer_b_grant_covers_conversations",
    "list_remote_backups",
    "logger",
    "logging",
    "make_job_runner",
    "nightly_enabled",
    "nightly_failures",
    "nightly_retry_delay_secs",
    "nightly_run_witness",
    "nightly_sessions_enabled",
    "open_lock_file",
    "os",
    "other_install_ids",
    "re",
    "read_remote_label",
    "read_state",
    "record_nightly_failure",
    "redact_credentials",
    "redact_exfiltration_urls",
    "redact_log_via_context",
    "remembered_archives",
    "restore_download",
    "retention_keep",
    "retention_owned_keys",
    "retention_unclaimed",
    "retention_unrecorded",
    "run_sessions_backup",
    "run_snapshot_backup",
    "sanitize_label",
    "scheduled_sessions_blocked_code",
    "scheduled_sessions_blocked_reason",
    "secrets",
    "sel",
    "sessions_layer_b_enabled",
    "set_install_label",
    "set_nightly",
    "set_nightly_sessions",
    "set_retention_keep",
    "set_sessions_layer_b",
    "signal_stop",
    "snapshot",
    "snapshot_main",
    "snapshot_redact",
    "sqlite3",
    "stat",
    "state_db_candidates",
    "storage",
    "sys",
    "tarfile",
    "tempfile",
    "threading",
    "uploaded_keys",
    "uploaded_objects",
    "uploaded_versions",
    "urllib",
    "uuid",
    "write_state",
)

_ABSENT = object()


def _shared_names() -> list[str]:
    """Every non-dunder name held by the facade and a part, or by two parts."""
    seen: dict[str, int] = {}
    for module in (backup, *PARTS):
        for name in vars(module):
            if not name.startswith("__"):
                seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def _holders(name: str) -> list[ModuleType]:
    """The modules whose own namespace binds ``name``."""
    return [module for module in (backup, *PARTS) if name in vars(module)]


def _bindings(name: str, holders: list[ModuleType]) -> list[object]:
    """What each of *holders* binds ``name`` to, ``None`` where it binds nothing."""
    return [vars(module).get(name) for module in holders]


def _put_back(name: str, original: object, holders: list[ModuleType]) -> None:
    """Write *original* straight into every holder.

    The cleanup of a case that drives the facade's undo: it goes around that undo, so a
    regression in it fails the one case instead of every later test in the process.
    """
    for module in holders:
        setattr(module, name, original)


@contextmanager
def _patched(kind: str, name: str, value: object) -> Iterator[None]:
    """Patch ``backup.<name>`` to *value* the way *kind* spells it, for one block."""
    if kind == "monkeypatch.setattr":
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backup, name, value)
            yield
    else:
        with mock.patch.object(backup, name, new=value):
            yield


def _unwinds_like_a_flat_module(
    name: str, holders: list[ModuleType], steps: list[tuple[str, object]], label: str
) -> None:
    """Enter *steps* outermost first; every exit must restore what its enter replaced.

    That is what each spelling does on a module that binds the name itself, whatever the
    values: the same object written twice, or the original written back inside a patch.
    """
    if not steps:
        return
    (kind, value), rest = steps[0], steps[1:]
    before = _bindings(name, holders)
    with _patched(kind, name, value):
        assert _bindings(name, holders) == [value] * len(holders), label
        _unwinds_like_a_flat_module(name, holders, rest, label)
        assert _bindings(name, holders) == [value] * len(holders), label
    assert _bindings(name, holders) == before, label


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


class TestTheSurfaceSurvivesTheSplit:
    def test_the_frozen_inventory_is_not_empty(self) -> None:
        # An emptied list would make the case below pass while checking nothing.
        assert len(FROZEN_NAMES) > 200

    @pytest.mark.parametrize("name", FROZEN_NAMES)
    def test_every_name_the_module_bound_still_resolves_to_its_owners_object(
        self, name: str
    ) -> None:
        value = getattr(backup, name, _ABSENT)
        assert value is not _ABSENT, f"backup.{name} no longer resolves"
        for module in _holders(name):
            assert (
                vars(module)[name] is value
            ), f"backup.{name} answers a different object than {module.__name__} holds"

    def test_the_part_order_is_the_facades_and_covers_the_package(self) -> None:
        # The facade resolves a read from the first part in this order that holds the
        # name, so a part missing from it is a part whose names the facade cannot reach.
        assert backup._PART_MODULES == tuple(part.__name__ for part in PARTS)
        on_disk = {path.stem for path in _PARTS_DIR.glob("*.py") if path.stem != "__init__"}
        assert on_disk == {part.__name__.rpartition(".")[2] for part in PARTS}

    def test_an_exported_name_is_read_from_its_owner_on_every_access(self) -> None:
        # Not bound in the facade, so a value written straight into the owner is what
        # the facade answers. (It is not what the owner's IMPORTERS see -- only a write
        # through the facade reaches them -- which is why tests patch the facade.)
        assert "_state_path" in backup._EXPORTS
        assert "_state_path" not in vars(backup)
        replacement = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(state, "_state_path", replacement)
            assert backup._state_path is replacement

    def test_a_name_a_part_rebinds_stays_live_through_the_facade(self) -> None:
        # ``_run_sequence`` is advanced by ``global`` inside the ledger, so a copy bound
        # in the facade would freeze at import. A ``global`` rebind writes its module's
        # namespace directly and never reaches ``_Facade``, so every one -- in a part or
        # in the facade -- must name a binding no other module holds.
        rebound = set()
        for module in (backup, *PARTS):
            for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Global):
                    rebound.update((name, module) for name in node.names)
        assert rebound == {("_run_sequence", ledger)}
        for name, part in rebound:
            assert [module for module in _holders(name)] == [part]
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(ledger, "_run_sequence", 41)
            assert backup._run_sequence == 41

    def test_a_missing_name_is_an_attribute_error(self) -> None:
        # ``hasattr``, ``getattr(..., default)`` and ``mock.patch`` all rely on it.
        assert not hasattr(backup, "_add_tree_by_name")
        with pytest.raises(AttributeError):
            backup.no_such_backup_name  # noqa: B018

    def test_dir_lists_the_exported_names(self) -> None:
        assert set(FROZEN_NAMES) <= set(dir(backup))

    def test_a_star_import_carries_exactly_the_public_names_of_the_inventory(self) -> None:
        # ``__all__`` is derived; the machinery binds only private names, so nothing it
        # needs leaks into a star importer's namespace and nothing public goes missing.
        assert set(backup.__all__) == {name for name in FROZEN_NAMES if not name.startswith("_")}

    def test_the_type_checker_sees_every_exported_name(self) -> None:
        # ``__getattr__`` is hidden from the checker, so the names it serves at run time
        # are declared to it under ``TYPE_CHECKING``; a name missing there would type as
        # an error at a correct call site, and an extra one would hide a stale name.
        tree = ast.parse(Path(backup.__file__).read_text(encoding="utf-8"))
        declared: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.If) and ast.unparse(node.test) == "_typing.TYPE_CHECKING":
                for stmt in node.body:
                    if isinstance(stmt, ast.ImportFrom) and stmt.module:
                        declared.update((alias.name, stmt.module) for alias in stmt.names)
        assert declared == {name: holders[0] for name, holders in backup._EXPORTS.items()}


# ---------------------------------------------------------------------------
# One symbol per name, and writes that reach every binding of it
# ---------------------------------------------------------------------------


class TestOneNamespaceForWrites:
    @pytest.mark.parametrize("name", _shared_names())
    def test_every_module_holding_a_name_holds_the_same_object(self, name: str) -> None:
        values = {id(vars(module)[name]) for module in _holders(name)}
        assert (
            len(values) == 1
        ), f"{name} names different objects in {[m.__name__ for m in _holders(name)]}"

    @pytest.mark.parametrize("name", _shared_names())
    def test_a_write_through_the_facade_reaches_every_holder_and_is_undone(self, name: str) -> None:
        holders = [module for module in _holders(name) if module is not backup]
        original = getattr(backup, name)
        sentinel = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backup, name, sentinel)
            assert getattr(backup, name) is sentinel
            for module in holders:
                assert vars(module)[name] is sentinel, f"{module.__name__}.{name} was missed"
        assert getattr(backup, name) is original
        for module in holders:
            assert vars(module)[name] is original, f"{module.__name__}.{name} not restored"

    def test_mock_patch_of_an_exported_name_restores_every_holder(self) -> None:
        # ``mock.patch`` sees a name the facade does not bind as non-local, so its exit
        # deletes the name and writes the original back; both halves go through here.
        name = "_locked_state_update"
        holders = [module for module in _holders(name) if module is not backup]
        assert name in backup._EXPORTS and len(holders) > 1
        original = getattr(backup, name)
        with mock.patch.object(backup, name) as fake:
            for module in holders:
                assert vars(module)[name] is fake
        for module in holders:
            assert vars(module)[name] is original

    def test_every_nesting_of_the_patch_harnesses_unwinds_like_a_flat_module(self) -> None:
        # Four deep over ``mock.patch`` and ``monkeypatch.setattr``, with the original
        # and one fake as the values, so a patch writing back what an enclosing patch
        # replaced, or the same object twice, is covered. Each harness restores what it
        # read at its own enter, so no nesting depends on the facade pairing anything.
        name = "_locked_state_update"
        holders = [module for module in _holders(name) if module is not backup]
        original = getattr(backup, name)
        fake = object()
        kinds = ("mock.patch", "monkeypatch.setattr")
        steps = [(kind, value) for kind in kinds for value in (original, fake)]
        try:
            for depth in range(1, 5):
                for sequence in itertools.product(steps, repeat=depth):
                    label = " > ".join(
                        f"{kind}({'original' if value is original else 'fake'})"
                        for kind, value in sequence
                    )
                    _unwinds_like_a_flat_module(name, holders, list(sequence), label)
        finally:
            _put_back(name, original, holders)

    def test_create_true_on_a_forwarded_name_deletes_it_from_every_holder(self) -> None:
        # The one patch spelling the facade cannot undo: ``mock.patch`` sees a forwarded
        # name as non-local, and under ``create=True`` its exit is the delete alone. The
        # guard in ``TestPatchSpellings`` keeps that spelling out of the suite, with this
        # case its one allowlisted site: it keeps the guard's premise true, and fails
        # the day the facade can undo it.
        name = "_locked_state_update"
        holders = [module for module in _holders(name) if module is not backup]
        assert name in backup._EXPORTS and len(holders) > 1
        original = getattr(backup, name)
        try:
            with mock.patch.object(backup, name, create=True):
                pass
            assert _bindings(name, holders) == [None] * len(holders)
        finally:
            _put_back(name, original, holders)

    def test_shadowing_a_builtin_through_the_facade_reaches_every_part(self) -> None:
        # One namespace for writes includes the builtins a module can shadow.
        def fake_sorted(*args, **kwargs):
            return []

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backup, "sorted", fake_sorted, raising=False)
            for module in (backup, *PARTS):
                assert vars(module)["sorted"] is fake_sorted
        for module in (backup, *PARTS):
            assert "sorted" not in vars(module)

    def test_patching_the_facades_sys_does_not_redirect_its_own_resolution(self) -> None:
        # ``backup.sys`` is part of the surface (the conversation store lookup reads
        # ``sys.platform``), and patching it must not change where the facade finds its
        # owners: the machinery reads ``sys`` through a private alias.
        def fake_checked(*args, **kwargs):
            return "[]"

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backup, "sys", mock.MagicMock(platform="win32"))
            assert backup._state_path is state._state_path
            patched.setattr(backup, "_checked", fake_checked)
            assert catalog._checked is fake_checked

    def test_a_delete_and_restore_through_the_facade_round_trips(self) -> None:
        # ``mock.patch`` undoes a name the facade does not bind by DELETING it and then
        # writing the original back, so both halves have to reach every holder.
        name = "_locked_state_update"
        holders = [module for module in _holders(name) if module is not backup]
        assert len(holders) > 1
        original = getattr(backup, name)
        with pytest.MonkeyPatch.context() as patched:
            patched.delattr(backup, name)
            assert not hasattr(backup, name)
            for module in holders:
                assert name not in vars(module)
        for module in holders:
            assert vars(module)[name] is original

    def test_the_patch_form_the_suite_uses_reaches_a_consumer_in_another_part(
        self, tmp_path: Path
    ) -> None:
        # The behaviour, not only the bindings. The state path is defined in ``state``
        # and read there; the update transaction is defined in ``state`` and CALLED from
        # ``nightly``, through the binding ``nightly`` imported. A patch on the facade
        # has to reach that second binding for the spy to see the call at all.
        calls = []
        real_update = state._locked_state_update

        def spy(mutate, on_in_lock_failure=None):
            calls.append(mutate)
            return real_update(mutate, on_in_lock_failure)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
            patched.setattr(backup, "_locked_state_update", spy)
            backup.set_nightly("111122223333", True)
            assert len(calls) == 1
            assert (tmp_path / "backup.json").is_file()
            assert backup.nightly_enabled("111122223333") is True
            assert backup.last_runs("111122223333") == {}
        assert nightly._locked_state_update is real_update

    def test_the_locks_are_single_objects_in_the_documented_order(self) -> None:
        # One lock per invariant, shared by every holder, and the retention gate is not
        # the run lock -- holding the run lock across a purge would stall the status read.
        for name in ("_run_lock", "_unpersisted_lock", "_fallback_lock", "_RETENTION_GATE"):
            assert len({id(vars(module)[name]) for module in _holders(name)}) == 1
        assert backup._RETENTION_GATE is not backup._run_lock
        source = Path(state.__file__).read_text(encoding="utf-8")
        note = source.index("# -- LOCK ORDER")
        assert note < source.index("def _state_lock(")
        assert "_RETENTION_GATE -> state sidecar FILE lock -> _run_lock -> leaf locks" in source

    def test_every_part_logs_through_the_facades_logger(self) -> None:
        # Log routing, filters and ``caplog`` captures key on the facade's name.
        for part in PARTS:
            if "logger" in vars(part):
                assert part.logger is backup.logger
        assert backup.logger.name == backup.__name__


# ---------------------------------------------------------------------------
# Layering and placement
# ---------------------------------------------------------------------------


def _part_imports(part: ModuleType) -> set[str]:
    """Every engine module one part imports: a lower part, or the facade."""
    return _engine_imports(Path(part.__file__).read_text(encoding="utf-8"))


def _engine_imports(source: str) -> set[str]:
    """Every engine module a part's source imports: a lower part, or the facade.

    Walks the whole tree, so an import inside a function counts, and resolves a
    relative import against the part's own package, so ``from ..backup import x`` and
    ``from . import retention`` are seen for what they name.
    """
    backend = backup.__name__.rpartition(".")[0]
    engine = {backup.__name__, *(p.__name__ for p in PARTS)}
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, _PARTS_PACKAGE)
            for alias in node.names:
                for candidate in (f"{base}.{alias.name}", base):
                    if candidate in engine:
                        found.add(candidate)
                        break
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in engine)
    assert backend not in found
    return found


class TestLayering:
    def test_the_import_reader_sees_every_spelling_of_an_engine_import(self) -> None:
        # A reader that saw only absolute module strings would pass a part importing
        # the facade lazily or relatively, which is the violation it exists to catch.
        source = (
            "from . import retention\n"
            "from .state import _state_path\n"
            "def f():\n"
            "    from ..backup import _add_tree\n"
            "    from .. import backup\n"
            f"import {_PARTS_PACKAGE}.ledger\n"
        )
        assert _engine_imports(source) == {
            f"{_PARTS_PACKAGE}.retention",
            f"{_PARTS_PACKAGE}.state",
            f"{_PARTS_PACKAGE}.ledger",
            backup.__name__,
        }

    def test_a_part_imports_only_parts_below_it_and_never_the_facade(self) -> None:
        order = [part.__name__ for part in PARTS]
        for index, part in enumerate(PARTS):
            imported = _part_imports(part)
            assert backup.__name__ not in imported, f"{part.__name__} imports the facade"
            later = imported - set(order[:index])
            assert later == set(), f"{part.__name__} imports a part at or above it: {later}"

    @pytest.mark.parametrize(
        "name",
        [
            # Declared link-screen sites, keyed to this file by path.
            "_add_tree",
            "_conversation_scratch_parent",
            "_kiro_cli_conversation_db",
            "restore_download",
            # The redaction-sink row names this module as the backup push boundary.
            "run_snapshot_backup",
            "run_sessions_backup",
            "_publish_label",
            "_export_cli_conversations",
        ],
    )
    def test_the_constructs_other_gates_pin_to_backup_py_stay_there(self, name: str) -> None:
        assert getattr(backup, name).__module__ == backup.__name__
        assert name in vars(backup)

    def test_every_outbound_put_is_made_by_the_facade(self) -> None:
        # The parts decide; the facade is where archive and label bytes leave.
        for part in PARTS:
            tree = ast.parse(Path(part.__file__).read_text(encoding="utf-8"))
            puts = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "put_file"
            ]
            assert puts == [], f"{part.__name__} calls put_file at line(s) {puts}"

    def test_no_text_io_in_any_part_omits_an_encoding(self) -> None:
        # The same rule ``test_snapshot_partial_bundle_manifest.py`` holds ``backup.py``
        # to, over the code that moved out of it: a JSON state file read with the
        # locale codepage is refused on a Windows host for any non-ASCII byte.
        offenders = []
        for part in PARTS:
            tree = ast.parse(Path(part.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("read_text", "write_text")
                    and not any(kw.arg == "encoding" for kw in node.keywords)
                ):
                    offenders.append(f"{Path(part.__file__).name}:{node.lineno}")
        assert offenders == []

    def test_the_facade_resolves_owners_by_name_not_by_module_object(self) -> None:
        # A table of module objects is a second place a module is stored; a part
        # purged and imported again would then be reached through the stale copy.
        for value in (backup._EXPORTS, backup._ALSO_HELD):
            for holders in value.values():
                assert all(isinstance(holder, str) for holder in holders)
        assert inspect.getsource(backup._part).count("importlib.import_module(") == 1
        assert importlib.import_module(backup._PART_MODULES[0]) is egress_text


# ---------------------------------------------------------------------------
# The patch spellings the suite may use on the facade
# ---------------------------------------------------------------------------

#: Directory names a repository-wide scan never enters.
_NOT_SCANNED = frozenset({".worktrees", "node_modules", ".venv", "__pycache__"})

#: The patch callables by the dotted path they resolve to: which one, and the index of
#: ``create`` among its positional parameters.
_PATCH_CALLABLES = {
    "unittest.mock.patch": ("patch", 3),
    "unittest.mock.patch.object": ("object", 4),
    "unittest.mock.patch.multiple": ("multiple", 2),
}

#: ``patch.multiple`` keywords that configure the patch rather than name an attribute.
_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})

#: What a hit names when the patched attribute cannot be read off the source.
_DYNAMIC = "<dynamic>"

#: The one deliberate ``create=True`` patch of a forwarded name, keyed by file and the
#: test enclosing it: the premise case, which shows that such a patch still deletes the
#: name and puts every holder back itself.
_ALLOWED_CREATE_TRUE = frozenset(
    {
        (
            "test/test_aws_control_backup_composition_contract.py",
            "TestOneNamespaceForWrites."
            "test_create_true_on_a_forwarded_name_deletes_it_from_every_holder",
        )
    }
)

#: A value an expression may denote: a dotted ``path`` (a module, or an attribute reached
#: from one), a ``str``, or the known leading ``prefix`` of a string.
_Value = tuple[str, str]


class _Hit(NamedTuple):
    function: str
    name: str
    line: int


class _Resolver:
    """What the names in one module's source may denote, read off its AST alone.

    A name is bound by an import, or by a plain assignment in its function's scope or an
    enclosing one, followed to a fixed point; a name bound more than once may denote any
    of its values. An expression the reader cannot follow denotes nothing.
    """

    def __init__(self, tree: ast.Module, module: str | None, reexports: frozenset[str]) -> None:
        self._tree = tree
        self._package = module.rpartition(".")[0] if module else None
        self._reexports = reexports
        self._parents: dict[ast.AST, ast.AST] = {}
        self._bindings: dict[ast.AST, dict[str, list[ast.AST | frozenset[_Value]]]] = {}
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
                stack.append(child)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, path in self._imported(node):
                    self._bind(node, name, frozenset({("path", path)}))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._bind(node, target.id, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self._bind(node, node.target.id, node.value)
        self._following: set[tuple[int, str]] = set()
        self._known: dict[tuple[int, str], set[_Value]] = {}

    def _imported(self, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (alias.asname, alias.name) if alias.asname else (alias.name.split(".")[0],) * 2
                for alias in node.names
            ]
        module = node.module or ""
        if node.level:
            if self._package is None:
                return []
            module = importlib.util.resolve_name("." * node.level + module, self._package)
        return [(alias.asname or alias.name, f"{module}.{alias.name}") for alias in node.names]

    def _bind(self, statement: ast.AST, name: str, value: ast.AST | frozenset[_Value]) -> None:
        self._bindings.setdefault(self.scope_of(statement), {}).setdefault(name, []).append(value)

    def scope_of(self, node: ast.AST) -> ast.AST:
        """The function whose body holds *node*, or the module; a decorator is outside."""
        child, parent = node, self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                decorators = getattr(parent, "decorator_list", [])
                if not any(child is decorator for decorator in decorators):
                    return parent
            child, parent = parent, self._parents.get(parent)
        return self._tree

    def function_of(self, node: ast.AST) -> str:
        """The dotted class and function names enclosing *node*, ``<module>`` for none."""
        names = []
        parent = self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(parent.name)
            parent = self._parents.get(parent)
        return ".".join(reversed(names)) or "<module>"

    def module_names(self) -> list[str]:
        return list(self._bindings.get(self._tree, {}))

    def is_facade(self, value: _Value) -> bool:
        kind, text = value
        return kind == "path" and (text == backup.__name__ or text in self._reexports)

    def values(self, expr: ast.AST | None, scope: ast.AST) -> set[_Value]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            return {("str", expr.value)} if isinstance(expr.value, str) else set()
        if isinstance(expr, ast.Name):
            return self._name(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            found: set[_Value] = set()
            for value in self.values(expr.value, scope):
                if value[0] == "path":
                    found.add(("path", f"{value[1]}.{expr.attr}"))
                    if expr.attr == "__name__" and self.is_facade(value):
                        found.add(("str", backup.__name__))
            return found
        if isinstance(expr, ast.Call) and len(expr.args) == 1:
            if ("path", "importlib.import_module") in self.values(expr.func, scope):
                return {
                    ("path", text)
                    for kind, text in self.values(expr.args[0], scope)
                    if kind == "str"
                }
            return set()
        if isinstance(expr, ast.JoinedStr):
            return self._joined(expr.values, scope)
        if isinstance(expr, ast.FormattedValue):
            plain = expr.conversion == -1 and expr.format_spec is None
            return self.values(expr.value, scope) if plain else set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self._joined([expr.left, expr.right], scope)
        return set()

    def _joined(self, parts: list[ast.expr], scope: ast.AST) -> set[_Value]:
        text = ""
        for part in parts:
            values = self.values(part, scope)
            strings = {value for kind, value in values if kind == "str"}
            prefixes = {value for kind, value in values if kind == "prefix"}
            if len(strings) == 1 and not prefixes:
                text += strings.pop()
                continue
            if len(prefixes) == 1 and not strings:
                text += prefixes.pop()
            return {("prefix", text)} if text else set()
        return {("str", text)}

    def _name(self, name: str, scope: ast.AST) -> set[_Value]:
        key = (id(scope), name)
        if key in self._known:
            return self._known[key]
        if key in self._following:
            return set()
        self._following.add(key)
        try:
            found: set[_Value] = set()
            for binding_scope in self._chain(scope):
                bound = self._bindings.get(binding_scope, {}).get(name)
                if bound is not None:
                    for value in bound:
                        found |= (
                            value
                            if isinstance(value, frozenset)
                            else self.values(value, binding_scope)
                        )
                    break
            self._known[key] = found
            return found
        finally:
            self._following.discard(key)

    def _chain(self, scope: ast.AST) -> Iterator[ast.AST]:
        while scope is not self._tree:
            yield scope
            scope = self.scope_of(scope)
        yield self._tree

    def hits(self, call: ast.Call) -> list[str]:
        """The forwarded names *call* patches with ``create`` not literally ``False``."""
        scope = self.scope_of(call)
        callable_ = next(
            (
                _PATCH_CALLABLES[text]
                for kind, text in self.values(call.func, scope)
                if text in _PATCH_CALLABLES
            ),
            None,
        )
        if callable_ is None:
            return []
        kind, create_at = callable_
        keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        create = keywords.get(
            "create", call.args[create_at] if len(call.args) > create_at else None
        )
        if create is None or (isinstance(create, ast.Constant) and create.value is False):
            return []
        target = keywords.get("target", call.args[0] if call.args else None)
        targets = self.values(target, scope)
        prefix = backup.__name__ + "."
        found: set[str] = set()
        if kind == "patch":
            for value_kind, text in targets:
                rest = text[len(prefix) :] if text.startswith(prefix) else None
                if rest is None or "." in rest:
                    continue
                found.add(rest if value_kind == "str" else _DYNAMIC)
        elif kind == "object":
            if any(self.is_facade(value) for value in targets):
                attribute = keywords.get("attribute", call.args[1] if len(call.args) > 1 else None)
                names = {
                    text
                    for value_kind, text in self.values(attribute, scope)
                    if value_kind == "str"
                }
                found = names if names else {_DYNAMIC}
        elif any(self.is_facade(value) for value in targets) or ("str", backup.__name__) in targets:
            found = {name for name in keywords if name not in _MULTIPLE_OPTIONS}
            if any(keyword.arg is None for keyword in call.keywords):
                found.add(_DYNAMIC)
        return sorted(name for name in found if name == _DYNAMIC or name in backup._EXPORTS)


def _module_name(path: Path) -> str | None:
    """The dotted import name of a file under ``src/``; a file elsewhere has none."""
    relative = path.relative_to(_REPO_ROOT)
    if relative.parts[0] != "src":
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _in_a_test_directory(path: Path) -> bool:
    return any(part.endswith("tests") for part in path.relative_to(_REPO_ROOT).parts[:-1])


@functools.lru_cache(maxsize=1)
def _facade_reexports() -> frozenset[str]:
    """Dotted paths that name the facade through a production module importing it.

    Read off the source the same way the guard reads a test, to a fixed point, so a module
    re-exporting another's alias is covered, and a new re-export needs no edit here.
    """
    candidates: list[tuple[str, ast.Module]] = []
    for path in (_REPO_ROOT / "src").rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if _NOT_SCANNED.intersection(relative.parts) or _in_a_test_directory(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "backup" in text and ("aws_control" in text or "aws_control" in relative.parts):
            candidates.append((_module_name(path) or "", ast.parse(text)))
    found: frozenset[str] = frozenset()
    while True:
        grown = set(found)
        for module, tree in candidates:
            resolver = _Resolver(tree, module, found)
            for name in resolver.module_names():
                if any(resolver.is_facade(v) for v in resolver.values(ast.Name(id=name), tree)):
                    grown.add(f"{module}.{name}")
        if grown == found:
            return found
        found = frozenset(grown)


def _may_pass_create(call: ast.Call) -> bool:
    """Whether *call* could hand a patch a ``create`` that is not literally ``False``.

    A ``create`` keyword that is not the literal ``False``, or three positional arguments
    or more to a callable spelled ``patch``, ``object`` or ``multiple`` -- where
    ``create`` would sit positionally. Only these calls are worth resolving.
    """
    for keyword in call.keywords:
        if keyword.arg == "create":
            return not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
    func = call.func
    spelled = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return len(call.args) >= 3 and spelled in ("patch", "object", "multiple")


def _create_true_patches_of_forwarded_names(source: str, module: str | None = None) -> list[_Hit]:
    """Every ``mock.patch`` of a forwarded name in *source* whose ``create`` is not
    literally ``False``.

    Any spelling the reader can resolve counts: ``patch``, ``patch.object`` and
    ``patch.multiple`` reached through any import alias, called or used as a decorator,
    with a positional or keyword target and attribute, a keyword ``create`` -- or a
    positional one to a callable spelled ``patch``, ``object`` or ``multiple`` -- and a
    string target built from the facade's name. A patch of the facade whose attribute it cannot
    resolve is a ``<dynamic>`` hit, never a pass. A name the facade binds itself is
    safe -- ``mock.patch`` sees it as local and writes the original back.
    """
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _may_pass_create(node)
    ]
    if not calls:
        return []
    resolver = _Resolver(tree, module, _facade_reexports())
    return [
        _Hit(resolver.function_of(call), name, call.lineno)
        for call in calls
        for name in resolver.hits(call)
    ]


def _patch_sources() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every test module of the repository that may patch the facade.

    ``test/`` and every directory under ``src/`` whose name ends in ``tests`` (``tests``,
    ``container_tests``), read once as text and handed to the AST reader only when it
    names both ``create`` and ``backup``.
    """
    paths = [*(_REPO_ROOT / "test").rglob("*.py")]
    paths += [path for path in (_REPO_ROOT / "src").rglob("*.py") if _in_a_test_directory(path)]
    for path in paths:
        if _NOT_SCANNED.intersection(path.relative_to(_REPO_ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "create" in text and "backup" in text:
            yield path, text


#: Imports most reader cases share.
_CASE_IMPORTS = (
    "from unittest import mock\n" "from kiro_crew.apps.builtins.aws_control.backend import backup\n"
)

#: ``(id, source, module, expected names)`` for the reader: each form the guard must
#: catch beside a spelling of it the guard must leave alone. ``@EXP@`` is a forwarded
#: name, ``@BOUND@`` one the facade binds itself, ``@FACADE@`` the facade's dotted name.
_READER_CASES: list[tuple[str, str, str | None, list[str]]] = [
    (
        "patch.object",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "create=False",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', create=False)\n",
        None,
        [],
    ),
    ("no create", _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@')\n", None, []),
    (
        "create positionally",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', mock.DEFAULT, None, True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "False positionally",
        _CASE_IMPORTS + "mock.patch.object(backup, '@EXP@', mock.DEFAULT, None, False)\n",
        None,
        [],
    ),
    (
        "create not a literal",
        _CASE_IMPORTS + "flag = True\nmock.patch.object(backup, '@EXP@', create=flag)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a name the facade binds",
        _CASE_IMPORTS + "mock.patch.object(backup, '@BOUND@', create=True)\n",
        None,
        [],
    ),
    (
        "a name no module holds",
        _CASE_IMPORTS + "mock.patch.object(backup, '_redact_for_upload', create=True)\n",
        None,
        [],
    ),
    (
        "another module",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(config, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "another module's .backup",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(config.backup, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "patch as an alias",
        "from unittest.mock import patch as P\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nP.object(backup, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a local function named patch",
        "from kiro_crew.apps.builtins.aws_control.backend import backup\ndef patch(*a, **k):\n    return None\npatch.object(backup, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "mock as an alias",
        "from unittest import mock as M\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nM.patch.object(backup, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock as an alias",
        "import unittest.mock as um\num.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "unittest.mock by its path",
        "import unittest.mock\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nunittest.mock.patch.multiple(backup, create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a third-party mock",
        "import mock\nfrom kiro_crew.apps.builtins.aws_control.backend import backup\nmock.patch.object(backup, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "as a decorator",
        _CASE_IMPORTS
        + "@mock.patch.object(backup, '@EXP@', create=True)\ndef test_x(fake):\n    pass\n",
        None,
        ["@EXP@"],
    ),
    (
        "a decorator with create=False",
        _CASE_IMPORTS
        + "@mock.patch.object(backup, '@EXP@', create=False)\ndef test_x(fake):\n    pass\n",
        None,
        [],
    ),
    (
        "a dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a deeper dotted string",
        "from unittest import mock\nmock.patch('@FACADE@.storage.find_drive', create=True)\n",
        None,
        [],
    ),
    (
        "another dotted string",
        "from unittest import mock\nmock.patch('kiro_crew.config.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of __name__",
        _CASE_IMPORTS + "mock.patch(f'{backup.__name__}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another __name__",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch(f'{config.__name__}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of a constant",
        "from unittest import mock\nFACADE = '@FACADE@'\nmock.patch(f'{FACADE}.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string of another constant",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\nmock.patch(f'{OTHER}.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a constant concatenated",
        "from unittest import mock\nFACADE = '@FACADE@'\nmock.patch(FACADE + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "__name__ concatenated",
        _CASE_IMPORTS + "mock.patch(backup.__name__ + '.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "another constant concatenated",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\nmock.patch(OTHER + '.@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword target and attribute",
        _CASE_IMPORTS + "mock.patch.object(target=backup, attribute='@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "keyword target elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\nmock.patch.object(target=config, attribute='@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "keyword string target",
        "from unittest import mock\nmock.patch(target='@FACADE@.@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an unresolved attribute",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(backup, attr, create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "an unresolved attribute, create=False",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(backup, attr, create=False)\n",
        None,
        [],
    ),
    (
        "a resolved local attribute",
        _CASE_IMPORTS
        + "def test_x():\n    name = '@EXP@'\n    mock.patch.object(backup, name, create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an f-string it cannot finish",
        _CASE_IMPORTS
        + "def test_x(attr):\n    mock.patch(f'{backup.__name__}.{attr}', create=True)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs in patch.multiple",
        _CASE_IMPORTS + "def test_x(kw):\n    mock.patch.multiple(backup, create=True, **kw)\n",
        None,
        [_DYNAMIC],
    ),
    (
        "**kwargs elsewhere",
        _CASE_IMPORTS
        + "from kiro_crew import config\ndef test_x(kw):\n    mock.patch.multiple(config, create=True, **kw)\n",
        None,
        [],
    ),
    (
        "patch.multiple of a dotted string",
        "from unittest import mock\nmock.patch.multiple('@FACADE@', create=True, @EXP@=None)\n",
        None,
        ["@EXP@"],
    ),
    (
        "patch.multiple of another string",
        "from unittest import mock\nmock.patch.multiple('kiro_crew.config', create=True, @EXP@=None)\n",
        None,
        [],
    ),
    (
        "an assigned re-export",
        "from unittest import mock\nfrom kiro_crew.apps.builtins.aws_control import hooks\ndef test_x():\n    bk = hooks.backup_mod\n    mock.patch.object(bk, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "an assigned other attribute",
        "from unittest import mock\nfrom kiro_crew.apps.builtins.aws_control import hooks\ndef test_x():\n    bk = hooks.backup_now\n    mock.patch.object(bk, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "another module's re-export",
        "from unittest import mock\nfrom kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod\nalias = routes_mod.backup_mod\nmock.patch.object(alias, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module",
        "import importlib\nfrom unittest import mock\nfacade = importlib.import_module('@FACADE@')\nmock.patch.object(facade, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "import_module elsewhere",
        "import importlib\nfrom unittest import mock\nother = importlib.import_module('kiro_crew.config')\nmock.patch.object(other, '@EXP@', create=True)\n",
        None,
        [],
    ),
    (
        "a chain of assignments",
        _CASE_IMPORTS + "a = backup\nb = a\nmock.patch.object(b, '@EXP@', create=True)\n",
        None,
        ["@EXP@"],
    ),
    (
        "a relative import in a package",
        "from unittest import mock\nfrom ..backend import backup\nmock.patch.object(backup, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.aws_control.tests.test_case",
        ["@EXP@"],
    ),
    (
        "a relative import elsewhere",
        "from unittest import mock\nfrom ..crew import backup\nmock.patch.object(backup, '@EXP@', create=True)\n",
        "kiro_crew.apps.builtins.aws_control.tests.test_case",
        [],
    ),
]


def _case(template: str) -> str:
    return (
        template.replace("@EXP@", "_locked_state_update")
        .replace("@BOUND@", "_add_tree")
        .replace("@FACADE@", backup.__name__)
    )


class TestPatchSpellings:
    def test_the_case_names_are_what_they_claim(self) -> None:
        assert "_locked_state_update" in backup._EXPORTS
        assert "_add_tree" in vars(backup) and "_add_tree" not in backup._EXPORTS
        assert "_redact_for_upload" not in backup._EXPORTS and not hasattr(
            backup, "_redact_for_upload"
        )
        assert _facade_reexports() >= {
            "kiro_crew.apps.builtins.aws_control.hooks.backup_mod",
            "kiro_crew.apps.builtins.aws_control.backend.routes.backup_mod",
        }

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _READER_CASES],
        ids=[case[0] for case in _READER_CASES],
    )
    def test_the_reader_flags_every_spelling_and_only_those(
        self, source: str, module: str | None, expected: list[str]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that flagged a
        # safe spelling would stop a patch that undoes cleanly.
        hits = _create_true_patches_of_forwarded_names(_case(source), module)
        assert [hit.name for hit in hits] == [_case(name) for name in expected]

    def test_no_test_patches_a_forwarded_name_with_create_true(self) -> None:
        # ``mock.patch`` undoes a name the facade forwards by deleting it, and under
        # ``create=True`` the delete is the whole undo: the name is gone from every
        # engine module for the rest of the run, and a later test fails far from here.
        # The scan must find exactly the allowlisted premise case, so the allowlist can
        # neither hide a second site nor outlive the one it names.
        hits = [
            (path.relative_to(_REPO_ROOT).as_posix(), hit)
            for path, text in _patch_sources()
            for hit in _create_true_patches_of_forwarded_names(text, _module_name(path))
        ]
        found = {(path, hit.function) for path, hit in hits}
        unexpected = [
            f"{path}:{hit.line} {hit.function} patches {hit.name}"
            for path, hit in hits
            if (path, hit.function) not in _ALLOWED_CREATE_TRUE
        ]
        assert found == _ALLOWED_CREATE_TRUE, (
            "mock.patch(..., create=True) of a name backend.backup forwards to its owner "
            "deletes that name from every engine module when the patch exits. Drop "
            "create=True (the name exists) or patch it with monkeypatch.setattr: "
            f"{unexpected}; allowlisted but not found: {sorted(_ALLOWED_CREATE_TRUE - found)}"
        )
