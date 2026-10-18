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
  several hundred patch sites. A write into one part reaches no other holder, so no
  test outside this file's premise cases patches a part directly.
* The owners form one acyclic stack under the facade, none of them importing it,
  and the constructs other gates pin by path stay where those gates key them.

This file pins the surface, the one namespace for writes and the layering, and holds the
premise cases that the two repository-wide patch guards allowlist by this file's path and
test name. Both guards live in
``test_aws_control_backup_composition_contract_patch_guards.py``:
``TestPatchSpellings`` fails on a ``mock.patch`` of a name the facade forwards that passes
a ``create`` other than the literal ``False``, and ``TestNoTestPatchesAPart`` fails on a
write into a part's namespace. ``PARTS``, ``_REPO_ROOT`` and ``_PARTS_PACKAGE``, which
both files read, live in ``aws_control_backup_composition_contract_helpers.py``.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import itertools
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from aws_control_backup_composition_contract_helpers import (
    _PARTS_PACKAGE,
    _REPO_ROOT,
    PARTS,
)

from kiro_crew.apps.builtins.aws_control.backend import backup
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import (
    catalog,
    egress_text,
    ledger,
    nightly,
    state,
)

_BACKEND = _REPO_ROOT / "src/kiro_crew/apps/builtins/aws_control/backend"
_PARTS_DIR = _BACKEND / "backup_parts"

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
    "JOB_PARAM_CALLER",
    "JOB_PARAM_PROFILE",
    "JOB_PARAM_REGION",
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
    "RETENTION_LAST_SWEEP_STATE_KEY",
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
    "ScheduledUploadWithheld",
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
    "_TEARDOWN_REFUSAL",
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
    "_authorization_ending",
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
    "_filed_by_the_gate",
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
    "_record_last_sweep",
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
    "retention_last_sweep",
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
        ("name", "home"),
        [
            # Declared link-screen sites, keyed by path in ``test/link_screen_sites.py``;
            # each home is the module that baseline names for the site.
            ("_add_tree", f"{_PARTS_PACKAGE}.archive_inputs"),
            ("_conversation_scratch_parent", f"{_PARTS_PACKAGE}.conversations"),
            ("_kiro_cli_conversation_db", f"{_PARTS_PACKAGE}.conversations"),
            ("restore_download", f"{_PARTS_PACKAGE}.restore"),
            # The redaction-sink row names the facade as the backup push boundary, and
            # ``security_posture.NON_EGRESS_REDACTION_MODULES`` lists the conversation
            # export's part as an internal partition behind it.
            ("run_snapshot_backup", backup.__name__),
            ("run_sessions_backup", backup.__name__),
            ("_publish_label", backup.__name__),
            ("_export_cli_conversations", f"{_PARTS_PACKAGE}.conversations"),
        ],
    )
    def test_the_constructs_other_gates_pin_to_backup_py_stay_there(
        self, name: str, home: str
    ) -> None:
        assert getattr(backup, name).__module__ == home
        assert name in vars(sys.modules[home])

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

    def test_the_facade_resolves_owners_by_name_not_by_module_object(self) -> None:
        # A table of module objects is a second place a module is stored; a part
        # purged and imported again would then be reached through the stale copy.
        for value in (backup._EXPORTS, backup._ALSO_HELD):
            for holders in value.values():
                assert all(isinstance(holder, str) for holder in holders)
        assert inspect.getsource(backup._part).count("importlib.import_module(") == 1
        assert importlib.import_module(backup._PART_MODULES[0]) is egress_text
