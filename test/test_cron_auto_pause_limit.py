"""Per-job auto-pause limit: ``auto_pause_after_failures``.

A cron job auto-pauses after a run of consecutive failures. The default is
``_AUTO_PAUSE_THRESHOLD``; these tests pin the per-job override: a custom limit
pauses at that count, ``0`` never
pauses, the value survives a reload, legacy records keep the default, and every
write surface (store, MCP tools) validates it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew import cron as cron_facade
from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronSchedule, CronService
from kiro_crew.cron_service.model import (
    _AUTO_PAUSE_MAX,
    _NEVER_PAUSE_BACKOFF_BASE_SECS,
    _NEVER_PAUSE_BACKOFF_MAX_SECS,
    validate_auto_pause_after_failures,
)
from kiro_crew.cron_service.schedule import compute_next_run_ts, is_due, next_wake_secs
from kiro_crew.mcp_cron import _call_tool_inner, _call_tool_locally, _list_tools
from kiro_crew.validation import MCP_CRON_SCHEMAS, ValidationError, validate_tool_args


def _job(**kwargs: object) -> CronJob:
    return CronJob(
        id="j",
        name="n",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=60),
        **kwargs,  # type: ignore[arg-type]
    )


class TestRecordFailureHonoursLimit:
    def test_default_limit_is_unchanged(self) -> None:
        job = _job()
        assert job.auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD
        for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
            job.record_failure()
        assert job.auto_paused is False
        job.record_failure()
        assert job.auto_paused is True and job.enabled is False

    def test_custom_limit_pauses_at_that_count(self) -> None:
        job = _job(auto_pause_after_failures=2)
        job.record_failure()
        assert job.auto_paused is False
        job.record_failure()
        assert job.auto_paused is True and job.enabled is False

    def test_higher_limit_keeps_firing_past_the_default(self) -> None:
        job = _job(auto_pause_after_failures=20)
        for _ in range(19):
            job.record_failure()
        assert job.auto_paused is False and job.enabled is True
        job.record_failure()
        assert job.auto_paused is True

    def test_zero_never_auto_pauses(self) -> None:
        job = _job(auto_pause_after_failures=0)
        for _ in range(_AUTO_PAUSE_THRESHOLD * 20):
            job.record_failure()
        assert job.consecutive_failures == _AUTO_PAUSE_THRESHOLD * 20
        assert job.auto_paused is False and job.enabled is True
        assert job.auto_pause_limit() is None

    @pytest.mark.parametrize("bad", [-1, "5", 2.5, True, None, 10001, 10**9])
    def test_malformed_in_memory_value_falls_back_to_default(self, bad: object) -> None:
        # No writer can produce these; a hand-edited store must not silently
        # disable the safety net.
        job = _job(auto_pause_after_failures=bad)
        assert job.auto_pause_limit() == _AUTO_PAUSE_THRESHOLD


class TestNeverPauseBackoff:
    """A never-pause job keeps a residual bound: it backs off instead of pausing."""

    def _failing(self, failures: int, *, limit: int = 0, last: float = 1_000_000.0) -> CronJob:
        job = _job(auto_pause_after_failures=limit)
        for _ in range(failures):
            job.record_failure()
        job.last_run_ts = last
        return job

    def test_no_backoff_below_the_default_limit(self) -> None:
        job = self._failing(_AUTO_PAUSE_THRESHOLD - 1)
        assert job.failure_backoff_until() is None
        assert is_due(job, job.last_run_ts + 60)

    def test_backoff_starts_where_the_default_would_have_paused(self) -> None:
        job = self._failing(_AUTO_PAUSE_THRESHOLD)
        until = job.last_run_ts + _NEVER_PAUSE_BACKOFF_BASE_SECS
        assert job.failure_backoff_until() == until
        # The every=60 schedule is due, the backoff is not over.
        assert not is_due(job, job.last_run_ts + 60)
        assert not is_due(job, until - 1)
        assert is_due(job, until)

    def test_backoff_doubles_and_is_capped(self) -> None:
        once_more = self._failing(_AUTO_PAUSE_THRESHOLD + 1)
        assert once_more.failure_backoff_until() == (
            once_more.last_run_ts + 2 * _NEVER_PAUSE_BACKOFF_BASE_SECS
        )
        many = self._failing(_AUTO_PAUSE_THRESHOLD + 500)
        assert many.failure_backoff_until() == many.last_run_ts + _NEVER_PAUSE_BACKOFF_MAX_SECS

    def test_a_raised_limit_backs_off_like_zero(self) -> None:
        # An agent may set any limit up to the cap through MCP. Without this, a
        # failing every=60 job at limit 10000 would run about 7 days at full rate.
        for limit in (_AUTO_PAUSE_THRESHOLD + 1, 50, _AUTO_PAUSE_MAX):
            job = self._failing(_AUTO_PAUSE_THRESHOLD, limit=limit)
            assert job.enabled
            until = job.last_run_ts + _NEVER_PAUSE_BACKOFF_BASE_SECS
            assert job.failure_backoff_until() == until
            assert not is_due(job, job.last_run_ts + 60)

    def test_a_limit_at_or_below_the_default_never_backs_off(self) -> None:
        for limit in (1, _AUTO_PAUSE_THRESHOLD):
            job = self._failing(_AUTO_PAUSE_THRESHOLD + 3, limit=limit)
            assert job.failure_backoff_until() is None

    def test_success_resets_the_backoff(self) -> None:
        job = self._failing(_AUTO_PAUSE_THRESHOLD + 3)
        job.record_success()
        assert job.failure_backoff_until() is None

    def test_timer_and_display_wait_for_the_backoff(self) -> None:
        job = self._failing(_AUTO_PAUSE_THRESHOLD)
        now = job.last_run_ts + 60
        until = job.last_run_ts + _NEVER_PAUSE_BACKOFF_BASE_SECS
        assert next_wake_secs([job], set(), now) == pytest.approx(until - now)
        assert compute_next_run_ts(job, now) == until

    def test_one_shot_timer_waits_for_the_backoff(self) -> None:
        job = self._failing(_AUTO_PAUSE_THRESHOLD)
        job.schedule = CronSchedule(kind="at", at_ts=job.last_run_ts - 3600)
        now = job.last_run_ts + 120
        until = job.last_run_ts + _NEVER_PAUSE_BACKOFF_BASE_SECS
        assert not is_due(job, now)
        assert next_wake_secs([job], set(), now) == pytest.approx(until - now)

    def test_a_raised_limit_still_pauses_at_that_limit(self) -> None:
        # The help says a raised limit backs off "until it succeeds or reaches a
        # nonzero limit": the backoff does not stop the pause at the limit.
        job = self._failing(6, limit=7)
        assert job.enabled
        job.record_failure()
        assert not job.enabled

    def test_cron_schedule_display_moves_to_the_first_slot_after_backoff(self) -> None:
        job = self._failing(_AUTO_PAUSE_THRESHOLD)
        job.schedule = CronSchedule(kind="cron", cron_expr="* * * * *")
        job.timezone = "UTC"
        now = job.last_run_ts + 30
        nxt = compute_next_run_ts(job, now)
        assert nxt is not None and nxt >= job.failure_backoff_until()

    def _quarter_hourly_failing(self) -> CronJob:
        # Last run 08:40 UTC; 7 failures put the backoff end on the 09:00 slot.
        job = self._failing(7, last=datetime(2026, 10, 26, 8, 40, tzinfo=timezone.utc).timestamp())
        job.schedule = CronSchedule(kind="cron", cron_expr="*/15 * * * *")
        job.timezone = "UTC"
        return job

    def test_cron_display_keeps_a_slot_that_falls_exactly_on_the_backoff_end(self) -> None:
        job = self._quarter_hourly_failing()
        until = job.failure_backoff_until()
        assert until == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc).timestamp()
        assert is_due(job, until)
        assert compute_next_run_ts(job, job.last_run_ts + 60) == until

    def test_cron_display_keeps_the_minute_a_fractional_backoff_end_falls_in(self) -> None:
        # A cron run's last_run_ts is its completion time, so the end is rarely whole.
        job = self._quarter_hourly_failing()
        job.last_run_ts += 0.5  # backoff now ends half a second into the 09:00 slot
        until = job.failure_backoff_until()
        nxt = compute_next_run_ts(job, job.last_run_ts + 60)
        assert nxt == until and is_due(job, nxt)

    def test_a_late_backoff_end_in_a_matching_minute_is_woken_on_and_shown(self) -> None:
        # Ends 45 s into the 09:00 slot: past both the boundary wake and the 30 s poll.
        job = self._quarter_hourly_failing()
        job.last_run_ts += 45
        until = job.failure_backoff_until()
        assert until == datetime(2026, 10, 26, 9, 0, 45, tzinfo=timezone.utc).timestamp()
        assert not is_due(job, until - 15) and is_due(job, until)
        assert compute_next_run_ts(job, job.last_run_ts + 60) == until
        assert next_wake_secs([job], set(), until - 15) == pytest.approx(15)
        # Read inside that minute, before the end: the end is still the next run.
        assert compute_next_run_ts(job, until - 15) == until

    def test_a_backoff_end_between_slots_adds_no_wake(self) -> None:
        job = self._quarter_hourly_failing()
        job.last_run_ts += 5 * 60  # ends 09:05; the 08:45 boundary wake stays first
        now = datetime(2026, 10, 26, 8, 46, tzinfo=timezone.utc).timestamp()
        assert next_wake_secs([job], set(), now) == pytest.approx(14 * 60)

    def test_cron_display_moves_a_backoff_end_between_slots_to_the_next_slot(self) -> None:
        job = self._quarter_hourly_failing()
        job.last_run_ts += 5 * 60  # backoff now ends at 09:05, between slots
        nxt = compute_next_run_ts(job, job.last_run_ts + 60)
        assert nxt == datetime(2026, 10, 26, 9, 15, tzinfo=timezone.utc).timestamp()

    def test_cron_display_on_a_skipped_backoff_end_date_moves_to_the_next_allowed_date(
        self,
    ) -> None:
        job = self._quarter_hourly_failing()
        job.skip_dates = ["2026-10-26"]
        nxt = compute_next_run_ts(job, job.last_run_ts + 60)
        assert nxt == datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc).timestamp()
        assert is_due(job, nxt)

    def test_cron_display_with_an_out_of_range_backoff_end_has_no_next_run(self) -> None:
        # A millisecond last_run_ts puts the backoff end past the year datetime can hold.
        job = self._quarter_hourly_failing()
        now = job.last_run_ts + 60
        job.last_run_ts *= 1000
        assert compute_next_run_ts(job, now) is None

    def test_cron_display_without_backoff_stays_strictly_future(self) -> None:
        job = self._quarter_hourly_failing()
        job.consecutive_failures = 0
        on_slot = datetime(2026, 10, 26, 8, 45, tzinfo=timezone.utc).timestamp()
        assert compute_next_run_ts(job, on_slot) == on_slot + 15 * 60


class TestNeverPauseAudit:
    @pytest.fixture()
    def audited(self, monkeypatch) -> list[dict]:
        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw: object) -> None:
                rows.append(kw)

        monkeypatch.setattr(cron_facade.sel, "sel", lambda: _Sel())
        return rows

    @staticmethod
    def _limit_rows(rows: list[dict]) -> list[dict]:
        return [r for r in rows if r.get("tool_kind") == "cron_auto_pause_limit"]

    def test_create_with_zero_is_audited(self, tmp_path: Path, audited: list[dict]) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="never", message="m", every_secs=60, auto_pause_after_failures=0)
        rows = self._limit_rows(audited)
        assert [r["outcome"] for r in rows] == ["never_pause_set"]
        assert rows[0]["metadata"]["job_id"] == job.id
        assert rows[0]["metadata"]["previous_limit"] is None

    def test_every_limit_past_the_default_is_audited_other_changes_are_not(
        self, tmp_path: Path, audited: list[dict]
    ) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="j", message="m", every_secs=60, auto_pause_after_failures=2)
        svc.update_job(job.id, auto_pause_after_failures=_AUTO_PAUSE_THRESHOLD)
        assert self._limit_rows(audited) == []
        svc.update_job(job.id, auto_pause_after_failures=_AUTO_PAUSE_MAX)
        svc.update_job(job.id, auto_pause_after_failures=0)
        svc.update_job(job.id, name="renamed")
        svc.update_job(job.id, auto_pause_after_failures=3)
        rows = self._limit_rows(audited)
        assert [r["outcome"] for r in rows] == [
            "limit_changed",
            "never_pause_set",
            "never_pause_cleared",
        ]
        assert rows[0]["metadata"]["previous_limit"] == _AUTO_PAUSE_THRESHOLD
        assert rows[0]["metadata"]["auto_pause_after_failures"] == _AUTO_PAUSE_MAX
        assert rows[1]["metadata"]["previous_limit"] == _AUTO_PAUSE_MAX
        assert rows[2]["metadata"]["auto_pause_after_failures"] == 3


class TestValidator:
    @pytest.mark.parametrize("value", [0, 1, 5, 10000, 7.0])
    def test_accepts_in_range(self, value: object) -> None:
        assert validate_auto_pause_after_failures(value) == int(value)  # type: ignore[call-overload]

    @pytest.mark.parametrize("value", [-1, 10001, True, False, 2.5, "x", None, float("inf")])
    def test_refuses_out_of_range_or_wrong_type(self, value: object) -> None:
        with pytest.raises(ValueError):
            validate_auto_pause_after_failures(value)


def test_published_mcp_descriptions_match_auto_pause_behavior() -> None:
    tools = {tool["name"]: tool for tool in _list_tools()}
    for tool_name in ("cron_add", "cron_update"):
        description = tools[tool_name]["inputSchema"]["properties"]["auto_pause_after_failures"][
            "description"
        ]
        assert f"0..{_AUTO_PAUSE_MAX}" in description
        assert f"default {_AUTO_PAUSE_THRESHOLD}" in description
        assert "0 = never auto-pause" in description
        # The backoff is the bound a raised or never limit leaves, so the
        # description names it as it behaves: 5 minutes, doubling, one-hour cap.
        assert "5 minutes apart" in description
        assert "up to one hour apart" in description
    assert validate_auto_pause_after_failures(0) == 0
    assert validate_auto_pause_after_failures(_AUTO_PAUSE_MAX) == _AUTO_PAUSE_MAX
    with pytest.raises(ValueError):
        validate_auto_pause_after_failures(_AUTO_PAUSE_MAX + 1)

    default_job = _job()
    for _ in range(_AUTO_PAUSE_THRESHOLD - 1):
        default_job.record_failure()
        assert default_job.auto_paused is False
    default_job.record_failure()
    assert default_job.auto_paused is True

    never_pause_job = _job(auto_pause_after_failures=0)
    for _ in range(_AUTO_PAUSE_THRESHOLD * 2):
        never_pause_job.record_failure()
    assert never_pause_job.auto_paused is False


def test_the_module_spec_states_the_current_limit_contract() -> None:
    spec = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "system-specs"
        / "modules"
        / "learn-cron-dashboard.md"
    ).read_text(encoding="utf-8")

    assert f"0..{_AUTO_PAUSE_MAX}" in spec
    assert f"`_AUTO_PAUSE_THRESHOLD` = {_AUTO_PAUSE_THRESHOLD}" in spec
    assert "`0` = never auto-pause" in spec


def test_the_cli_spec_names_the_flag_on_add_and_update() -> None:
    spec = (
        Path(__file__).resolve().parents[1] / "docs" / "system-specs" / "modules" / "cli.md"
    ).read_text(encoding="utf-8")
    row = next(line for line in spec.splitlines() if line.startswith("| `kirocrew cron add/"))

    assert "`--auto-pause-after N`" in row
    assert "`0` = never" in row
    assert "also on `cron update`" in row


class TestStore:
    def test_create_update_and_reload(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="m", every_secs=60, auto_pause_after_failures=0)
        assert job.auto_pause_after_failures == 0

        reloaded = CronService(base_dir=tmp_path)
        reloaded._load()
        assert reloaded.get_job(job.id).auto_pause_after_failures == 0

        reloaded.update_job(job.id, auto_pause_after_failures=12)
        again = CronService(base_dir=tmp_path)
        again._load()
        assert again.get_job(job.id).auto_pause_after_failures == 12

    def test_reset_to_default_after_reload_sticks(self, tmp_path: Path) -> None:
        # The default limit is omitted from the record, so a reloaded custom
        # value must not be kept as an unknown field and written back over it.
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="flaky", message="m", every_secs=60, auto_pause_after_failures=0)

        reloaded = CronService(base_dir=tmp_path)
        reloaded._load()
        reloaded.update_job(job.id, auto_pause_after_failures=_AUTO_PAUSE_THRESHOLD)

        again = CronService(base_dir=tmp_path)
        again._load()
        assert again.get_job(job.id).auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD
        assert "auto_pause_after_failures" not in (tmp_path / "crons.json").read_text(
            encoding="utf-8"
        )

    def test_omitted_on_create_is_default(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="plain", message="m", every_secs=60)
        assert job.auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD

    def test_invalid_values_refused_without_mutation(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        with pytest.raises(ValueError):
            svc.add_job(name="bad", message="m", every_secs=60, auto_pause_after_failures=-1)
        job = svc.add_job(name="ok", message="m", every_secs=60)
        with pytest.raises(ValueError):
            svc.update_job(job.id, name="renamed", auto_pause_after_failures=10001)
        fresh = CronService(base_dir=tmp_path)
        fresh._load()
        stored = fresh.get_job(job.id)
        assert stored.name == "ok"
        assert stored.auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD

    def test_legacy_record_without_field_keeps_default(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="legacy", message="m", every_secs=60)
        store = next(p for p in tmp_path.rglob("crons.json"))
        data = json.loads(store.read_text())
        records = data["jobs"] if isinstance(data, dict) else data
        for rec in records:
            rec.pop("auto_pause_after_failures", None)
        store.write_text(json.dumps(data))
        fresh = CronService(base_dir=tmp_path)
        fresh._load()
        assert fresh.get_job(job.id).auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD

    def test_new_limit_does_not_lift_an_existing_pause(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._load()
        job = svc.add_job(name="paused", message="m", every_secs=60)
        for _ in range(_AUTO_PAUSE_THRESHOLD):
            job.record_failure()
        svc._merge_job_result(job)
        updated = svc.update_job(job.id, auto_pause_after_failures=0)
        assert updated is not None
        assert updated.auto_paused is True and updated.enabled is False

    def test_default_limit_is_not_written_to_the_store(self, tmp_path: Path) -> None:
        # Stores with no custom limit keep their pre-field bytes.
        svc = CronService(base_dir=tmp_path)
        svc._load()
        plain = svc.add_job(name="plain", message="m", every_secs=60)
        custom = svc.add_job(name="custom", message="m", every_secs=60, auto_pause_after_failures=0)
        store = next(p for p in tmp_path.rglob("crons.json"))
        data = json.loads(store.read_text())
        records = {r["id"]: r for r in (data["jobs"] if isinstance(data, dict) else data)}
        assert "auto_pause_after_failures" not in records[plain.id]
        assert records[custom.id]["auto_pause_after_failures"] == 0


class TestMcpTools:
    @pytest.fixture(autouse=True)
    def _cron_caller_is_named(self, named_cron_caller):
        """These tests exercise field handling; see ``named_cron_caller`` in conftest."""

    def _reload(self, tmp_path: Path, name: str) -> CronJob:
        matching = [j for j in CronService(base_dir=tmp_path).list_jobs() if j.name == name]
        assert len(matching) == 1
        return matching[0]

    def test_cron_add_and_update_set_the_limit(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"lim-{uuid.uuid4().hex[:8]}"
        result = _call_tool_locally(
            "cron_add",
            {"name": name, "message": "go", "every": 300, "auto_pause_after_failures": 20},
        )
        assert "Added job" in result
        job = self._reload(tmp_path, name)
        assert job.auto_pause_after_failures == 20

        result = _call_tool_locally(
            "cron_update", {"job_id": job.id, "auto_pause_after_failures": 9}
        )
        assert "Updated job" in result
        assert self._reload(tmp_path, name).auto_pause_after_failures == 9

    def test_cron_add_and_update_accept_never_pause_and_audit_it(
        self, monkeypatch, tmp_path
    ) -> None:
        # 0 is agent-settable like any other limit: the backoff bounds the spend
        # of a never-pause job the same way it bounds a limit of 10000, and the
        # SEL row records who turned the pause off.
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw: object) -> None:
                rows.append(kw)

        monkeypatch.setattr(cron_facade.sel, "sel", lambda: _Sel())
        # The update rows must name the session that made the write, not the job.
        import kiro_crew.mcp_cron as mcp_cron_mod

        monkeypatch.setattr(mcp_cron_mod, "_authz_session_key", lambda: "dashboard:chat-writer")
        name = f"lim-{uuid.uuid4().hex[:8]}"
        result = _call_tool_locally(
            "cron_add",
            {"name": name, "message": "go", "every": 300, "auto_pause_after_failures": 0},
        )
        assert "Added job" in result
        job = self._reload(tmp_path, name)
        assert job.auto_pause_after_failures == 0

        result = _call_tool_locally(
            "cron_update", {"job_id": job.id, "auto_pause_after_failures": 7}
        )
        assert "Updated job" in result
        result = _call_tool_locally(
            "cron_update", {"job_id": job.id, "auto_pause_after_failures": 0}
        )
        assert "Updated job" in result
        assert self._reload(tmp_path, name).auto_pause_after_failures == 0
        outcomes = [r["outcome"] for r in rows if r.get("tool_kind") == "cron_auto_pause_limit"]
        assert outcomes == ["never_pause_set", "never_pause_cleared", "never_pause_set"]
        updates = [r for r in rows if r.get("tool_kind") == "cron_auto_pause_limit"][1:]
        for row in updates:
            assert row["session_key"] == "dashboard:chat-writer"
            assert row["metadata"]["actor_session_key"] == "dashboard:chat-writer"

    def test_cron_update_out_of_range_is_an_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
        name = f"lim-{uuid.uuid4().hex[:8]}"
        _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 300})
        jid = self._reload(tmp_path, name).id
        result = _call_tool_inner("cron_update", {"job_id": jid, "auto_pause_after_failures": -3})
        assert result.startswith("Error")
        assert self._reload(tmp_path, name).auto_pause_after_failures == _AUTO_PAUSE_THRESHOLD

    @pytest.mark.parametrize("tool", ["cron_add", "cron_update"])
    def test_schema_bounds(self, tool: str) -> None:
        base = {"name": "n", "message": "m"} if tool == "cron_add" else {"job_id": "abc12345"}
        schema = MCP_CRON_SCHEMAS[tool]
        assert validate_tool_args({**base, "auto_pause_after_failures": 0}, schema)
        assert validate_tool_args({**base, "auto_pause_after_failures": 10000}, schema)
        for bad in (-1, 10001):
            with pytest.raises(ValidationError):
                validate_tool_args({**base, "auto_pause_after_failures": bad}, schema)
