"""The ``kiro_crew.cron`` facade over its ``kiro_crew.cron_service`` owners.

The cron subsystem's owners live in ``kiro_crew/cron_service/`` behind the
``kiro_crew.cron`` facade; this file pins what that split must not change:

* every name the module defined still resolves on ``kiro_crew.cron`` as the same
  object its owner holds, and ``CronService`` keeps every member callers use;
* a name tests patch on ``kiro_crew.cron`` still reaches the moved code that reads
  it, and the patch undoes cleanly;
* the ``crons.json`` bytes a save writes, and the schedule math, are what they were;
* the owners never import the facade at import time, and read through it only the
  declared seams.

The byte and schedule goldens were produced by the pre-split module.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

import kiro_crew.cron as cron
from kiro_crew.cron import CronJob, CronSchedule, CronService

_REPO = Path(__file__).resolve().parents[1]
_OWNER_DIR = _REPO / "src" / "kiro_crew" / "cron_service"

#: Every name the pre-split ``cron.py`` defined (functions, classes, constants).
_BASE_DEFINED = (
    "CronFolderLookup",
    "CronJob",
    "CronLoopSafetyError",
    "CronPendingMismatch",
    "CronSchedule",
    "CronService",
    "CronStoreBusy",
    "CronStoreUnreadable",
    "_AUTO_PAUSE_THRESHOLD",
    "_CHAT_FOLDER_NEEDS_PERSISTENT",
    "_CRONS_FILE",
    "_CRON_FOLDERS_FILE",
    "_CRON_STRING_FIELD_CAPS",
    "_DEFAULT_DIR",
    "_ENDING_ROUNDS",
    "_FILE_LOCK_POLL_SECS",
    "_FILE_LOCK_TIMEOUT_SECS",
    "_JITTER_DAILY_MAX",
    "_JITTER_HOURLY_MAX",
    "_JOB_TIMEOUT_SECS",
    "_MAX_SKIP_DATE_HORIZON_SECS",
    "_MAX_SKIP_DATE_LOOKAHEAD",
    "_MIN_INTERVAL_SECS",
    "_REAPER_INTERVAL",
    "_REAPER_RESET_TIMEOUT",
    "_RE_IN_DURATION",
    "_RunClaim",
    "_SKILL_TOKEN_RE",
    "_STORE_VERSION",
    "_STRICT_LOOP_SAFETY_ENV",
    "_SUBPROC_CLEANUP_ALLOWANCE_SECS",
    "_TIMER_POLL_SECS",
    "_UNIT_SECS",
    "_captured_template_id",
    "_compute_next_run_ts_raw",
    "_default_dir",
    "_gate_budget_allowance",
    "_humanize_cron",
    "_is_loadable_record",
    "_is_representable_number",
    "_job_from_record",
    "_job_tz",
    "_manual_run_refused",
    "_next_cron_boundary_ts",
    "_pool_queue_allowance",
    "_read_cron_folders",
    "_read_job_records",
    "_record_is_enabled",
    "_record_user_paused",
    "_loop_safety_warned",
    "_validate_cron_string_fields",
    "_vet_allowance",
    "agent_sequence_dispatches",
    "bind_cron_memory",
    "build_cron_session_context",
    "compute_next_run_ts",
    "cron_expr_matches",
    "cron_job_id_from_session_key",
    "cron_owner_matches",
    "cron_session_key_is_stable",
    "cron_store_lock",
    "dispatched_agents_from_disk",
    "effective_wake_budget",
    "enabled_count_from_disk",
    "format_schedule",
    "get_local_tz",
    "is_valid_skip_date",
    "is_valid_timezone",
    "job_agent_names_from_disk",
    "job_pause_state_from_disk",
    "load_cron_folders",
    "lookup_cron_folder_id",
    "parse_time_string",
    "referenced_skill_names",
    "resolve_cron_memory",
    "unhealthy_jobs_from_disk",
    "validate_cron_expr",
)

#: Imported names callers and tests reach THROUGH ``kiro_crew.cron`` (as a patch
#: target or an attribute path), which the facade therefore keeps bound.
_BASE_REACHED_IMPORTS = (
    "KiroCrewConfig",
    "admission_check",
    "asyncio",
    "config_dir",
    "cron_script",
    "datetime",
    "emit_counter",
    "published_config_timezone",
    "random",
    "sel",
    "time",
)

#: The names tests patch on ``kiro_crew.cron`` that code in an owner reads; each
#: owner reads them through the facade per call. The seam test below proves each
#: reaches its reader, and the import-direction test pins that no owner reads any
#: other name through the facade.
_SEAMS = frozenset(
    {
        "datetime",
        "get_local_tz",
        "published_config_timezone",
        "cron_expr_matches",
        "config_dir",
        "_record_is_enabled",
        "sel",
        "_JOB_TIMEOUT_SECS",
    }
)

#: ``CronService`` members of the pre-split class that callers or tests reach.
#: The five claim fences moved onto :class:`~kiro_crew.cron_service.claims.RunClaims`
#: (``self._runs``) and are pinned separately below.
_BASE_SERVICE_MEMBERS = (
    "__init__",
    "_ack_job_locked",
    "_adopt_job_locked",
    "_apply_loop_stall_breaker",
    "_arm_timer",
    "_audit_requested_batch_removal",
    "_audit_requested_removal",
    "_build_job",
    "_bump_grant_epochs_for",
    "_chat_folder_ids_locked",
    "_claim_run",
    "_claims",
    "_compute_jitter",
    "_drain_pending_removals_locked",
    "_drop_owner_if_parent_gone",
    "_effective_delay",
    "_enable_job_locked",
    "_end_run_processes",
    "_end_run_sessions",
    "_execute",
    "_execute_with_timeout",
    "_fence_run_keys",
    "_file_lock",
    "_force_reap",
    "_guard_off_event_loop",
    "_handles_of",
    "_is_due",
    "_load",
    "_loop_stall_breaker_verdict",
    "_merge_job_result",
    "_merge_terminal_state_locked",
    "_next_wake_secs",
    "_on_timer",
    "_owner_keys_locked",
    "_pause_for_loop_stall",
    "_persist_add_if_absent_locked",
    "_persist_add_locked",
    "_reaper_loop",
    "_record_fingerprint",
    "_release_children_of_removed",
    "_release_jobs_owned_by_locked",
    "_remove_job_locked",
    "_remove_job_rows",
    "_remove_jobs_by_owner_locked",
    "_remove_jobs_locked",
    "_reset_and_kill_once",
    "_reset_fingerprint",
    "_run_claimed_manual",
    "_run_job_isolated",
    "_run_session_keys",
    "_save",
    "_session_process_handles",
    "_sessions_under",
    "_sigkill_session",
    "_sigkill_sessions",
    "_snapshot",
    "_spawn_in_flight",
    "_sync",
    "_sync_for_write",
    "_synced_snapshot",
    "_tick_scan_locked",
    "_unack_job_locked",
    "_unreadable_error",
    "_update_job_locked",
    "_update_job_locked_kw",
    "ack_job",
    "ack_job_async",
    "active_session_keys",
    "add_job",
    "add_job_async",
    "add_job_if_absent",
    "add_job_if_absent_async",
    "add_managed_job",
    "adopt_job",
    "attach_run_task",
    "audit_one_shot_removal",
    "cancel",
    "chat_folder_ids_async",
    "clear_active_session_key",
    "count_enabled_from_disk",
    "create",
    "defer_removal",
    "discard_finished_run",
    "enable_job",
    "enable_job_async",
    "get_history",
    "get_job",
    "get_job_async",
    "is_running",
    "list_jobs",
    "list_jobs_async",
    "owner_keys_async",
    "raise_if_store_unreadable",
    "register_active_session_key",
    "release_jobs_owned_by",
    "remove_job",
    "remove_job_async",
    "remove_jobs",
    "remove_jobs_by_owner",
    "remove_jobs_by_owner_sync",
    "remove_jobs_sync",
    "run_job",
    "running_since",
    "set_refresh_callback",
    "start",
    "start_reaper",
    "status",
    "stop",
    "unack_job",
    "unack_job_async",
    "update_job",
    "update_job_async",
)


def _owner_modules() -> list[ModuleType]:
    return [
        importlib.import_module(f"kiro_crew.cron_service.{path.stem}")
        for path in sorted(_OWNER_DIR.glob("*.py"))
        if path.stem != "__init__"
    ]


class TestTheFacadeSurface:
    def test_every_name_the_module_defined_still_resolves(self) -> None:
        missing = [name for name in _BASE_DEFINED if not hasattr(cron, name)]
        assert not missing

    def test_every_import_callers_reach_through_it_still_resolves(self) -> None:
        missing = [name for name in _BASE_REACHED_IMPORTS if not hasattr(cron, name)]
        assert not missing

    def test_a_moved_name_is_the_owner_s_own_object(self) -> None:
        """One object per name: the facade binds what the owner defines, never a copy."""
        owners = {}
        for module in _owner_modules():
            for name, value in vars(module).items():
                if getattr(value, "__module__", None) == module.__name__:
                    owners[name] = value
        moved = [name for name in _BASE_DEFINED if name in owners]
        assert len(moved) >= 40  # a vacuity guard: the split moved the bulk of the module
        diverged = [name for name in moved if getattr(cron, name) is not owners[name]]
        assert not diverged

    def test_a_star_import_still_exposes_every_public_name(self, tmp_path: Path) -> None:
        probe = tmp_path / "cron_star_probe.py"
        probe.write_text("from kiro_crew.cron import *  # noqa: F401,F403\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("cron_star_probe", probe)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        public = [name for name in _BASE_DEFINED if not name.startswith("_")]
        assert [name for name in public if not hasattr(module, name)] == []

    def test_the_facade_is_a_plain_module(self) -> None:
        """No ``__getattr__`` and no ``__class__`` swap: every name is an ordinary binding.

        So ``mock.patch`` sees each name as local and undoes a patch by writing
        the original back, and the type checker sees every name statically.
        """
        assert type(sys.modules["kiro_crew.cron"]) is ModuleType
        assert "__getattr__" not in vars(cron)

    def test_the_service_keeps_every_member_callers_reach(self) -> None:
        assert [name for name in _BASE_SERVICE_MEMBERS if not hasattr(CronService, name)] == []

    def test_the_claim_fences_moved_onto_the_registry(self, tmp_path: Path) -> None:
        from kiro_crew.cron_service.claims import RunClaims

        svc = CronService(base_dir=tmp_path)
        assert isinstance(svc._runs, RunClaims)
        claim = svc._claim_run("j1", "manual")
        assert svc._claims is svc._runs.claims and svc._claims["j1"] is claim
        assert svc._runs.holds("j1", claim)
        assert svc.running_since("j1") == claim.claimed_at
        assert svc._runs.take("j1") is claim
        assert svc.running_since("j1") is None
        svc._runs.finish_taken("j1")
        assert not svc.is_running("j1")
        for gone in (
            "_holds_claim",
            "_release_claim",
            "_take_claim",
            "_finish_taken_claim",
            "_next_run_generation",
        ):
            assert not hasattr(CronService, gone), gone

    @pytest.mark.parametrize(
        "frame",
        [
            '  File "/venv/site-packages/kiro_crew/cron_service/schedule.py", line 9 in is_due',
            r'  File "C:\venv\Lib\site-packages\kiro_crew\cron_service\store.py", line 9 in f',
        ],
    )
    def test_a_stall_inside_an_owner_is_still_the_cron_surface(self, frame: str) -> None:
        """The loop-stall classifier names a stall in moved cron code ``cron``, as before the split.

        A dashboard request wedged in ``compute_next_run_ts`` has no ``cron.py``
        frame of its own once the helper lives in ``cron_service/``; the breaker
        and ``kirocrew doctor`` still read it as a cron stall.
        """
        from kiro_crew.stall_attribution import classify_surface, parse_frames

        dashboard = (
            '  File "/venv/site-packages/kiro_crew/dashboard/handlers/cron.py", line 1 in api'
        )
        assert classify_surface(parse_frames([frame, dashboard])) == "cron"

    def test_every_owner_logs_on_the_cron_channel(self) -> None:
        """Operators filter, and tests ``caplog``, on ``kiro_crew.cron``."""
        for module in _owner_modules():
            logger = vars(module).get("logger")
            if logger is not None:
                assert logger.name == "kiro_crew.cron", module.__name__


def _function_local_facade_imports(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Every ``from kiro_crew import cron as <alias>`` inside a function, with its alias."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module == "kiro_crew":
                for alias in inner.names:
                    if alias.name == "cron":
                        found.append((node, alias.asname or "cron"))
    return found


class TestTheOwnersReadTheSeamsThroughTheFacade:
    def test_no_owner_imports_the_facade_at_import_time(self) -> None:
        for path in sorted(_OWNER_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    assert not (
                        node.module == "kiro_crew" and any(a.name == "cron" for a in node.names)
                    ), path.name
                    assert node.module != "kiro_crew.cron", path.name
                if isinstance(node, ast.Import):
                    assert all(a.name != "kiro_crew.cron" for a in node.names), path.name

    def test_only_the_declared_seams_are_read_through_it(self) -> None:
        read: set[str] = set()
        for path in sorted(_OWNER_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function, alias in _function_local_facade_imports(tree):
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == alias
                    ):
                        read.add(node.attr)
        assert read == _SEAMS

    def test_no_owner_reads_a_seam_from_its_own_namespace(self) -> None:
        """A seam read as a bare name would bypass a patch on the facade."""
        for path in sorted(_OWNER_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                annotations = {
                    id(n)
                    for arg in node.args.args + node.args.kwonlyargs
                    if arg.annotation is not None
                    for n in ast.walk(arg.annotation)
                }
                if node.returns is not None:
                    annotations |= {id(n) for n in ast.walk(node.returns)}
                bare = [
                    n.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Name)
                    and isinstance(n.ctx, ast.Load)
                    and n.id in _SEAMS
                    and id(n) not in annotations
                ]
                assert not bare, f"{path.name}:{node.name} reads {bare} without the facade"


class TestAPatchOnTheFacadeReachesTheMovedCode:
    """One case per seam: the patch lands on ``kiro_crew.cron``, the reader is an owner."""

    def test_datetime_and_get_local_tz_reach_parse_time_string(self) -> None:
        fixed = datetime(2026, 5, 14, 18, 0, 0, tzinfo=ZoneInfo("UTC"))

        class _Fixed(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        pacific = ZoneInfo("America/Los_Angeles")
        with (
            mock.patch.object(cron, "datetime", _Fixed),
            mock.patch.object(cron, "get_local_tz", return_value=("America/Los_Angeles", pacific)),
        ):
            ts = cron.parse_time_string("23:59")
        assert isinstance(ts, float)
        local = datetime.fromtimestamp(ts, tz=pacific)
        assert (local.date(), local.hour, local.minute) == (
            fixed.astimezone(pacific).date(),
            23,
            59,
        )

    def test_datetime_reaches_the_run_stamp(self) -> None:
        class _Stamp(datetime):
            @classmethod
            def fromtimestamp(cls, ts, tz=None):  # type: ignore[override]
                return datetime(2001, 2, 3, 4, 5, 6, tzinfo=ZoneInfo("UTC"))

        job = CronJob(id="j", name="n", message="m", timezone="UTC")
        with mock.patch.object(cron, "datetime", _Stamp):
            assert job._render_run_stamp(1.0) == " | 2001-02-03 04:05:06 UTC"

    def test_published_config_timezone_reaches_the_job_timezone(self) -> None:
        job = CronJob(id="j", name="n", message="m", timezone="")
        with mock.patch.object(cron, "published_config_timezone", return_value="Asia/Tokyo"):
            assert cron._job_tz(job) == ZoneInfo("Asia/Tokyo")
            assert cron.get_local_tz() == ("Asia/Tokyo", ZoneInfo("Asia/Tokyo"))
        with mock.patch.object(cron, "published_config_timezone", return_value=""):
            assert cron._job_tz(job) == ZoneInfo("UTC")

    def test_cron_expr_matches_reaches_the_due_decision(self) -> None:
        job = CronJob(
            id="j", name="n", message="m", schedule=CronSchedule(kind="cron", cron_expr="0 0 1 1 *")
        )
        now = datetime(2026, 6, 1, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
        assert CronService._is_due(job, now) is False
        with mock.patch.object(cron, "cron_expr_matches", return_value=True):
            assert CronService._is_due(job, now) is True

    def test_config_dir_reaches_the_store_readers_and_the_folders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "crons.json").write_text(
            json.dumps({"jobs": [{"id": "a", "name": "n", "message": "run $deploy-helper"}]}),
            encoding="utf-8",
        )
        (tmp_path / "cron_folders.json").write_text(
            json.dumps([{"id": "f1", "name": "Ops"}]), encoding="utf-8"
        )
        monkeypatch.setattr(cron, "config_dir", lambda: tmp_path)
        assert "deploy-helper" in cron.referenced_skill_names()
        assert cron.load_cron_folders() == [{"id": "f1", "name": "Ops"}]
        assert cron.lookup_cron_folder_id("ops").folder_id == "f1"

    def test_record_is_enabled_reaches_the_record_builder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = {"id": "a", "name": "n", "message": "m", "schedule": {"kind": "every"}}
        assert cron._job_from_record(record).enabled is True
        monkeypatch.setattr(cron, "_record_is_enabled", lambda j: False)
        assert cron._job_from_record(record).enabled is False

    def test_sel_reaches_the_auto_pause_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, object]] = []
        log = SimpleNamespace(log_tool_invocation=lambda **kw: calls.append(kw))
        monkeypatch.setattr(cron, "sel", SimpleNamespace(sel=lambda: log))
        job = CronJob(id="j", name="n", message="m", consecutive_failures=4)
        job.record_failure()
        assert job.auto_paused and calls and calls[0]["outcome"] == "auto_paused"

    def test_job_timeout_reaches_every_reader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cron, "_JOB_TIMEOUT_SECS", 7)
        unset = CronJob(id="j", name="n", message="m", timeout_secs=0)
        assert cron.effective_wake_budget(unset) == 7
        record = {"id": "a", "name": "n", "message": "m", "schedule": {"kind": "every"}}
        assert cron._job_from_record(record).timeout_secs == 7
        assert CronService._build_job("n", "m", every_secs=60).timeout_secs == 7


class TestAPatchUndoesCleanly:
    @pytest.mark.parametrize("create", [False, True])
    def test_mock_patch_restores_the_original(self, create: bool) -> None:
        original = cron.get_local_tz
        with mock.patch("kiro_crew.cron.get_local_tz", create=create) as fake:
            fake.return_value = ("UTC", ZoneInfo("UTC"))
            assert cron.parse_time_string("in 1 minute")
        assert cron.get_local_tz is original
        assert cron.parse_time_string("in 1 minute")

    def test_nested_monkeypatch_undo_restores_in_order(self) -> None:
        original = cron.published_config_timezone
        with pytest.MonkeyPatch.context() as outer:
            outer.setattr(cron, "published_config_timezone", lambda: "Asia/Tokyo")
            with pytest.MonkeyPatch.context() as inner:
                inner.setattr(cron, "published_config_timezone", lambda: "Europe/London")
                assert cron.get_local_tz()[0] == "Europe/London"
            assert cron.get_local_tz()[0] == "Asia/Tokyo"
        assert cron.published_config_timezone is original


_BASE_STORE_SHA256 = {
    "minimal": "4d60217b5d73a346618967791e75a15cef9d046b75e910a8eaa284fa0cbde2a1",
    "rich": "efcd7d48f3e1f1124c9f3bbb0c0110aef37344317b631655cf748de9b2cd3553",
}


def _full_record() -> dict[str, object]:
    """One entry carrying every stored field, set away from its default, in stored order."""
    return {
        "id": "abcd1234",
        "name": "nightly digest",
        "message": "summarise $deploy-helper",
        "schedule": {"kind": "cron", "every_secs": None, "at_ts": None, "cron_expr": "0 3 * * *"},
        "channel": "C123",
        "thread_ts": "1700000000.000100",
        "enabled": False,
        "user_paused": True,
        "auto_paused": True,
        "last_run_ts": 1772860000.5,
        "last_status": "error",
        "last_error": "boom",
        "created_ts": 1772800000.25,
        "delete_after_run": True,
        "last_result": "result text",
        "last_result_ts": 1772860001.0,
        "last_result_stamp": " | 2026-03-07 03:00:00 UTC",
        "context_enabled": True,
        "agent_id": "helper",
        "member_id": "member-1",
        "memory_store": "store-1",
        "execution_context": {"template_id": "helper", "k": [1, 2]},
        "approval_mode": "auto",
        "acked_items": ["seen one", "seen two"],
        "created_by": "U1",
        "source_preset": "error-digest",
        "source_template_prompt": "template prompt",
        "silent": True,
        "session_key": "dashboard:s1",
        "last_posted_hash": "abc",
        "consecutive_dupes": 2,
        "last_posted_at": 1772860002.0,
        "last_failure_hash": "def",
        "last_failure_at": 1772860003.0,
        "consecutive_failures": 3,
        "skip_dates": ["2026-12-25"],
        "timezone": "America/New_York",
        "persistent_session": False,
        "minimal_context": True,
        "hide_in_chat": True,
        "folder_id": "folder-1",
        "chat_folder_id": "chat-folder-1",
        "model": "some-model",
        "last_retry_count": 1,
        "last_retry_run_ts": 1772860000.5,
        "run_generation": 9,
        "agent_sequence": ["a1", "a2"],
        "env": {"K": "V"},
        "timeout_secs": 600,
        "strict_schedule": True,
        "script": "",
        "command": "echo hi",
        "timeout": 30,
        "secret_env": {"TOKEN": "vault-name"},
        "secret_env_pin": "pin",
        "secret_env_pending": {"OTHER": "vault-other"},
        "secret_env_pending_pin": "pending-pin",
        "secret_env_pending_ts": 1772860004.0,
    }


class TestTheStoreBytesAreUnchanged:
    @staticmethod
    def _save_fixture(fixture: str, tmp_path: Path) -> bytes:
        source = _REPO / "src" / "kiro_crew" / "tests_fixtures" / fixture / "crons.json"
        shutil.copy(source, tmp_path / "crons.json")
        svc = CronService(base_dir=tmp_path)
        svc._save()
        return (tmp_path / "crons.json").read_bytes()

    @pytest.mark.parametrize("fixture", sorted(_BASE_STORE_SHA256))
    def test_a_shipped_fixture_saves_to_the_same_bytes(self, fixture: str, tmp_path: Path) -> None:
        """The golden is the LF document; the file carries the platform's line ending.

        ``_save`` writes through ``atomic_write``'s universal newlines, so on
        Windows every ``\\n`` lands as ``\\r\\n``.
        """
        raw = self._save_fixture(fixture, tmp_path)
        lf = raw.replace(os.linesep.encode(), b"\n") if os.linesep != "\n" else raw
        assert hashlib.sha256(lf).hexdigest() == _BASE_STORE_SHA256[fixture]
        assert raw == lf.replace(b"\n", os.linesep.encode())

    def test_the_golden_holds_when_the_writer_emits_crlf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What a Windows save does, forced on any host: CRLF bytes, the same LF content."""
        from kiro_crew import atomic_write as atomic_write_module

        real = atomic_write_module.atomic_write

        def _crlf(path: Path, content: str, *args: Any, **kwargs: Any) -> Any:
            kwargs["newline"] = "\r\n"
            return real(path, content, *args, **kwargs)

        monkeypatch.setattr(atomic_write_module, "atomic_write", _crlf)
        raw = self._save_fixture("minimal", tmp_path)
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
        assert (
            hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() == _BASE_STORE_SHA256["minimal"]
        )

    def test_every_field_round_trips_byte_for_byte_in_stored_order(self, tmp_path: Path) -> None:
        document = {"version": 2, "jobs": [_full_record()]}
        (tmp_path / "crons.json").write_text(json.dumps(document), encoding="utf-8")
        svc = CronService(base_dir=tmp_path)
        assert [j.id for j in svc._jobs] == ["abcd1234"]
        svc._save()
        assert (tmp_path / "crons.json").read_text(encoding="utf-8") == json.dumps(
            document, indent=2
        )

    def test_the_encoder_and_decoder_are_inverse(self) -> None:
        document = {"version": 2, "jobs": [_full_record()]}
        jobs = cron.decode_jobs(json.dumps(document).encode("utf-8"))
        assert jobs is not None
        assert cron.encode_store(jobs) == json.dumps(document, indent=2)
        assert cron.decode_jobs(b"[]") is None
        with pytest.raises(ValueError):
            cron.decode_jobs(b"{not json")


#: ``(kind, value, timezone, skip dates, now, compute_next_run_ts, boundary, due,
#: format_schedule)``, produced by the pre-split module. The four nows straddle both
#: 2026 US DST changes; ``format_schedule`` is pinned for ``every`` only, since a cron
#: rendering depends on today's date.
_SCHEDULE_GOLDENS: list[tuple[str, object, str, list[str], float, object, object, bool, object]] = [
    (
        "cron",
        "30 2 * * *",
        "America/New_York",
        [],
        1772866800.0,
        1772868600.0,
        1772868600.0,
        False,
        None,
    ),
    (
        "cron",
        "30 2 * * *",
        "America/New_York",
        [],
        1772953200.0,
        1773037800.0,
        1773037800.0,
        False,
        None,
    ),
    (
        "cron",
        "30 2 * * *",
        "America/New_York",
        [],
        1793512800.0,
        1793518200.0,
        1793518200.0,
        False,
        None,
    ),
    (
        "cron",
        "30 2 * * *",
        "America/New_York",
        [],
        1793599200.0,
        1793604600.0,
        1793604600.0,
        False,
        None,
    ),
    (
        "cron",
        "0 1 * * *",
        "America/New_York",
        [],
        1772866800.0,
        1772949600.0,
        1772949600.0,
        False,
        None,
    ),
    (
        "cron",
        "0 1 * * *",
        "America/New_York",
        [],
        1772953200.0,
        1773032400.0,
        1773032400.0,
        False,
        None,
    ),
    (
        "cron",
        "0 1 * * *",
        "America/New_York",
        [],
        1793512800.0,
        1793599200.0,
        1793599200.0,
        True,
        None,
    ),
    (
        "cron",
        "0 1 * * *",
        "America/New_York",
        [],
        1793599200.0,
        1793685600.0,
        1793685600.0,
        True,
        None,
    ),
    (
        "cron",
        "*/15 * * * *",
        "Europe/London",
        [],
        1772866800.0,
        1772867700.0,
        1772867700.0,
        True,
        None,
    ),
    (
        "cron",
        "*/15 * * * *",
        "Europe/London",
        [],
        1772953200.0,
        1772954100.0,
        1772954100.0,
        True,
        None,
    ),
    (
        "cron",
        "*/15 * * * *",
        "Europe/London",
        [],
        1793512800.0,
        1793513700.0,
        1793513700.0,
        True,
        None,
    ),
    (
        "cron",
        "*/15 * * * *",
        "Europe/London",
        [],
        1793599200.0,
        1793600100.0,
        1793600100.0,
        True,
        None,
    ),
    (
        "cron",
        "0 9 * * 1-5",
        "Asia/Tokyo",
        ["2026-03-09", "2026-11-02"],
        1772866800.0,
        1773100800.0,
        1773014400.0,
        False,
        None,
    ),
    (
        "cron",
        "0 9 * * 1-5",
        "Asia/Tokyo",
        ["2026-03-09", "2026-11-02"],
        1772953200.0,
        1773100800.0,
        1773014400.0,
        False,
        None,
    ),
    (
        "cron",
        "0 9 * * 1-5",
        "Asia/Tokyo",
        ["2026-03-09", "2026-11-02"],
        1793512800.0,
        1793664000.0,
        1793577600.0,
        False,
        None,
    ),
    (
        "cron",
        "0 9 * * 1-5",
        "Asia/Tokyo",
        ["2026-03-09", "2026-11-02"],
        1793599200.0,
        1793664000.0,
        1793664000.0,
        False,
        None,
    ),
    ("cron", "0 0 1 * *", "", [], 1772866800.0, 1775001600.0, 1775001600.0, False, None),
    ("cron", "0 0 1 * *", "", [], 1772953200.0, 1775001600.0, 1775001600.0, False, None),
    ("cron", "0 0 1 * *", "", [], 1793512800.0, 1796083200.0, 1796083200.0, False, None),
    ("cron", "0 0 1 * *", "", [], 1793599200.0, 1796083200.0, 1796083200.0, False, None),
    ("every", 5400, "", [], 1772866800.0, 1772866800.0, None, True, "every 90m"),
    ("every", 5400, "", [], 1772953200.0, 1772953200.0, None, True, "every 90m"),
    ("every", 5400, "", [], 1793512800.0, 1793512800.0, None, True, "every 90m"),
    ("every", 5400, "", [], 1793599200.0, 1793599200.0, None, True, "every 90m"),
    (
        "every",
        86400,
        "America/Los_Angeles",
        [],
        1772866800.0,
        1772946400.0,
        None,
        False,
        "every 24h",
    ),
    (
        "every",
        86400,
        "America/Los_Angeles",
        [],
        1772953200.0,
        1772953200.0,
        None,
        True,
        "every 24h",
    ),
    (
        "every",
        86400,
        "America/Los_Angeles",
        [],
        1793512800.0,
        1793512800.0,
        None,
        True,
        "every 24h",
    ),
    (
        "every",
        86400,
        "America/Los_Angeles",
        [],
        1793599200.0,
        1793599200.0,
        None,
        True,
        "every 24h",
    ),
    ("at", 1772960000.0, "", [], 1772866800.0, 1772960000.0, None, False, None),
    ("at", 1772960000.0, "", [], 1772953200.0, 1772960000.0, None, False, None),
    ("at", 1772960000.0, "", [], 1793512800.0, None, None, True, None),
    ("at", 1772960000.0, "", [], 1793599200.0, None, None, True, None),
]


def _job_for(kind: str, value: object, tz: str, skips: list[str]) -> CronJob:
    schedule = CronSchedule(
        kind=kind,
        cron_expr=value if kind == "cron" else None,  # type: ignore[arg-type]
        every_secs=value if kind == "every" else None,  # type: ignore[arg-type]
        at_ts=value if kind == "at" else None,  # type: ignore[arg-type]
    )
    return CronJob(
        id="j",
        name="n",
        message="m",
        schedule=schedule,
        timezone=tz,
        skip_dates=list(skips),
        created_ts=1772800000.0,
        last_run_ts=1772860000.0,
    )


@pytest.mark.parametrize(
    "kind,value,tz,skips,now,expected_next,expected_boundary,expected_due,expected_fmt",
    _SCHEDULE_GOLDENS,
)
def test_the_schedule_math_is_unchanged(
    kind, value, tz, skips, now, expected_next, expected_boundary, expected_due, expected_fmt
) -> None:
    job = _job_for(kind, value, tz, skips)
    assert cron.compute_next_run_ts(job, now) == expected_next
    assert cron._next_cron_boundary_ts(job, now) == expected_boundary
    assert CronService._is_due(job, now) is expected_due
    if expected_fmt is not None:
        assert cron.format_schedule(job.schedule, tz or "UTC") == expected_fmt


def test_next_wake_skips_running_and_disabled_jobs() -> None:
    now = 1772866800.0
    every = _job_for("every", 5400, "", [])
    every.last_run_ts = now - 100
    at = _job_for("at", now + 10, "", [])
    at.id = "at"
    paused = _job_for("every", 60, "", [])
    paused.id, paused.enabled = "paused", False
    assert cron.next_wake_secs([every, at, paused], set(), now) == 10.0
    assert cron.next_wake_secs([every, at, paused], {"at"}, now) == 5300.0
    assert cron.next_wake_secs([paused], set(), now) is None


def test_jitter_stays_inside_its_bands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron.random, "uniform", lambda lo, hi: hi)
    assert CronService._compute_jitter(_job_for("every", 86400, "", [])) == cron._JITTER_DAILY_MAX
    assert CronService._compute_jitter(_job_for("every", 3600, "", [])) == cron._JITTER_HOURLY_MAX
    assert CronService._compute_jitter(_job_for("every", 600, "", [])) == 0.0
    assert CronService._compute_jitter(_job_for("cron", "*/15 * * * *", "", [])) == 0.0
    assert (
        CronService._compute_jitter(_job_for("cron", "0 3 * * *", "", [])) == cron._JITTER_DAILY_MAX
    )
    assert CronService._compute_jitter(_job_for("at", 1.0, "", [])) == 0.0
