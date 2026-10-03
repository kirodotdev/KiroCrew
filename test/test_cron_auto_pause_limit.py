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
from pathlib import Path

import pytest

from kiro_crew.cron import _AUTO_PAUSE_THRESHOLD, CronJob, CronSchedule, CronService
from kiro_crew.cron_service.model import _AUTO_PAUSE_MAX, validate_auto_pause_after_failures
from kiro_crew.mcp_cron import _call_tool_inner, _list_tools
from kiro_crew.validation import MCP_CRON_SCHEMAS, ValidationError, validate_tool_args


def _job(**kwargs: object) -> CronJob:
    return CronJob(
        id="j",
        name="n",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=60),
        **kwargs,  # type: ignore[arg-type]
    )


class TestInFlightLimitChange:
    """A limit committed while a run executes governs that run's failure.

    The run keeps its own copy of the job, so its record_failure() judges the
    old limit; apply_run_record re-judges it against the stored copy.
    """

    @staticmethod
    def _failed_run(run: CronJob) -> CronJob:
        run.begin_run()
        run.record_failure()
        return run

    def test_limit_raised_to_zero_mid_run_does_not_pause(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        run = _job(auto_pause_after_failures=2, consecutive_failures=1)
        self._failed_run(run)
        assert run.auto_paused is True  # the stale decision
        target = _job(auto_pause_after_failures=0, consecutive_failures=1)
        apply_run_record(target, run)
        assert target.auto_paused is False and target.enabled is True
        assert target.consecutive_failures == 2

    def test_limit_lowered_mid_run_pauses_on_that_failure(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        run = _job(auto_pause_after_failures=10, consecutive_failures=2)
        self._failed_run(run)
        assert run.auto_paused is False
        target = _job(auto_pause_after_failures=3, consecutive_failures=2)
        apply_run_record(target, run)
        assert target.auto_paused is True and target.enabled is False

    def test_pause_that_predates_the_run_is_kept(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        # A manual run of an already auto-paused job fails after the limit
        # was changed to 0: the limit change must not resume it.
        run = _job(auto_pause_after_failures=5, consecutive_failures=5, auto_paused=True)
        self._failed_run(run)
        assert run.auto_pause_tripped is False
        target = _job(auto_pause_after_failures=0, consecutive_failures=5, auto_paused=True)
        apply_run_record(target, run)
        assert target.auto_paused is True

    def test_resume_during_a_failed_run_of_a_paused_job_wins(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        # An auto-paused job is run manually, the user resumes it while that
        # run is in flight (auto_paused cleared, counter reset), and the run
        # then fails. The run's stale pause must not overwrite the resume.
        run = _job(auto_pause_after_failures=5, consecutive_failures=5, auto_paused=True)
        self._failed_run(run)
        assert run.auto_paused is True and run.auto_pause_tripped is False
        target = _job(auto_pause_after_failures=5, consecutive_failures=0)
        apply_run_record(target, run)
        assert target.auto_paused is False and target.enabled is True
        assert target.consecutive_failures == 0

    def test_resume_during_the_run_on_the_live_object_wins(self) -> None:
        import copy

        from kiro_crew.cron_service.execution import apply_run_record

        # Same race when the store's copy is the object the run used: the
        # failure was recorded first, then the resume cleared it in place.
        live = _job(auto_pause_after_failures=5, consecutive_failures=5, auto_paused=True)
        live.enabled = False
        self._failed_run(live)
        run = copy.copy(live)
        live.auto_paused = False
        live.enabled = True
        live.consecutive_failures = 0
        apply_run_record(live, run)
        assert live.auto_paused is False and live.enabled is True
        assert live.consecutive_failures == 0

    def test_resume_during_an_uncounted_run_of_a_paused_job_wins(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        # A manual run of an auto-paused job ends without recording a failure
        # (a partially blocked run skips record_failure) while a resume from
        # another process lands on the stored copy. The run's stale pause and
        # counter must not overwrite that resume either.
        run = _job(auto_pause_after_failures=5, consecutive_failures=5, auto_paused=True)
        run.begin_run()
        assert run.failure_recorded is False
        target = _job(auto_pause_after_failures=5, consecutive_failures=0)
        apply_run_record(target, run)
        assert target.auto_paused is False and target.enabled is True
        assert target.consecutive_failures == 0

    def test_uncounted_run_of_a_paused_job_keeps_the_pause(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        # Same uncounted run with no resume: the pause that predates it stays.
        run = _job(auto_pause_after_failures=5, consecutive_failures=5, auto_paused=True)
        run.begin_run()
        target = _job(auto_pause_after_failures=5, consecutive_failures=5, auto_paused=True)
        apply_run_record(target, run)
        assert target.auto_paused is True
        assert target.consecutive_failures == 5

    @pytest.mark.parametrize("limit", [5, 1])
    def test_resume_before_the_failure_on_the_live_object_keeps_the_reset(self, limit: int) -> None:
        import copy

        from kiro_crew.cron_service.execution import apply_run_record

        # A manual run of an auto-paused job shares the store's live object.
        # The resume lands before the run records its failure, so by the time
        # record_failure() runs the object is already unpaused with a zeroed
        # counter. The run started paused, so its failure predates the resume
        # and must neither count against the fresh attempts nor re-pause.
        live = _job(auto_pause_after_failures=limit, consecutive_failures=5, auto_paused=True)
        live.enabled = False
        live.begin_run()
        live.auto_paused = False
        live.enabled = True
        live.consecutive_failures = 0
        live.record_failure()
        run = copy.copy(live)
        apply_run_record(live, run)
        assert live.auto_paused is False and live.enabled is True
        assert live.consecutive_failures == 0

    def test_unchanged_limit_merges_the_run_decision(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        run = _job(auto_pause_after_failures=2, consecutive_failures=1)
        self._failed_run(run)
        target = _job(auto_pause_after_failures=2, consecutive_failures=1)
        apply_run_record(target, run)
        assert target.auto_paused is True and target.enabled is False

    def test_cleared_pause_re_enables_the_live_job_the_run_mutated(self) -> None:
        import copy

        from kiro_crew.cron_service.execution import apply_run_record

        # The store's copy is the same object the run's record_failure()
        # disabled (the merge's _sync() reloads only on an external change),
        # and the record is close_run's shallow copy of it. Clearing the pause
        # must also clear the `enabled=False` that pause wrote.
        live = _job(auto_pause_after_failures=2, consecutive_failures=1)
        self._failed_run(live)
        assert live.enabled is False
        run = copy.copy(live)
        live.auto_pause_after_failures = 0
        apply_run_record(live, run)
        assert live.auto_paused is False and live.enabled is True

    def test_cleared_pause_re_enables_when_target_is_the_run_object(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        job = _job(auto_pause_after_failures=2, consecutive_failures=1)
        self._failed_run(job)
        job.auto_pause_after_failures = 0
        apply_run_record(job, job)
        assert job.auto_paused is False and job.enabled is True

    def test_cleared_pause_keeps_a_distinct_disk_copy_enabled(self) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        run = _job(auto_pause_after_failures=2, consecutive_failures=1)
        self._failed_run(run)
        # A reloaded disk copy derives enabled from user_paused/auto_paused,
        # neither of which the run's pause reached: it is enabled on disk.
        target = _job(auto_pause_after_failures=0, consecutive_failures=1, enabled=True)
        apply_run_record(target, run)
        assert target.auto_paused is False and target.enabled is True

    def test_cleared_pause_leaves_a_user_paused_job_disabled(self) -> None:
        import copy

        from kiro_crew.cron_service.execution import apply_run_record

        live = _job(
            auto_pause_after_failures=2, consecutive_failures=1, user_paused=True, enabled=False
        )
        self._failed_run(live)
        run = copy.copy(live)
        live.auto_pause_after_failures = 0
        apply_run_record(live, run)
        assert live.auto_paused is False
        assert live.user_paused is True and live.enabled is False

    @pytest.mark.parametrize(
        ("delete_after_run", "fire_time_denied"),
        [(False, False), (True, True), (True, False)],
    )
    def test_cleared_pause_does_not_reenable_a_one_shot_at_job(
        self, delete_after_run: bool, fire_time_denied: bool
    ) -> None:
        from kiro_crew.cron_service.execution import apply_run_record

        job = CronJob(
            id="at",
            name="at",
            message="m",
            schedule=CronSchedule(kind="at", at_ts=1),
            delete_after_run=delete_after_run,
            fire_time_denied=fire_time_denied,
            auto_pause_after_failures=2,
            consecutive_failures=1,
        )
        self._failed_run(job)
        assert job.auto_pause_tripped is True and job.enabled is False

        # A concurrent update makes this run's pause stale. Retained one-shots
        # stay parked, while an ordinary delete-after-run one-shot stays disabled
        # until its consume completes; neither may be revived by the merge.
        job.auto_pause_after_failures = 0
        apply_run_record(job, job)
        assert job.auto_paused is False and job.enabled is False


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
        assert "0 = never" in description
        # 0 disables only the consecutive-failure pause: _pause_for_loop_stall
        # pauses a job without consulting auto_pause_limit().
        assert "loop-stall breaker still applies" in description
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
        result = _call_tool_inner(
            "cron_add",
            {"name": name, "message": "go", "every": 300, "auto_pause_after_failures": 0},
        )
        assert "Added job" in result
        job = self._reload(tmp_path, name)
        assert job.auto_pause_after_failures == 0

        result = _call_tool_inner("cron_update", {"job_id": job.id, "auto_pause_after_failures": 9})
        assert "Updated job" in result
        assert self._reload(tmp_path, name).auto_pause_after_failures == 9

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


def test_a_run_cancelled_in_its_jitter_sleep_merges_no_stale_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-run pause flags are reset before the jitter sleep.

    A run cancelled during jitter still merges its record in the ``finally``.
    If the flags were reset only once execution began, that merge would
    re-judge the PREVIOUS run's ``failure_recorded`` against the stored limit:
    a limit lowered below a banked streak would auto-pause a job whose run
    never executed.
    """
    import asyncio

    from kiro_crew import cron as cron_mod

    svc = CronService(base_dir=tmp_path)
    svc._load()
    job = svc.add_job(name="hourly", message="m", every_secs=3600, auto_pause_after_failures=5)
    # The previous run's leftovers: a recorded failure and a streak banked
    # while the limit was 0, then the limit was lowered to 5.
    job.consecutive_failures = 50
    job.failure_recorded = True
    job.auto_pause_tripped = False

    async def _cancel_in_jitter(_secs: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(CronService, "_compute_jitter", staticmethod(lambda _job: 30.0))
    monkeypatch.setattr(cron_mod.asyncio, "sleep", _cancel_in_jitter)

    async def _run() -> None:
        try:
            await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    stored = CronService(base_dir=tmp_path)
    stored._load()
    assert stored.get_job(job.id).auto_paused is False
