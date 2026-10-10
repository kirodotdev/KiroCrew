"""Property tests for CronSDK ownership enforcement.

Feature: app-sdk-gateway-hooks
Properties 3, 4, 5, 6: Cron job creation, ownership, filtering, cleanup.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.cron import CronService
from kiro_crew.llm_helpers import ToolApprovalPolicy


def _run(value: Any) -> Any:
    """Passthrough for the synchronous CronSDK mutation API.

    The public ``CronSDK`` mutation methods (``add_job`` / ``remove_job`` /
    ``update_job`` / ``remove_all``) are synchronous (they preserve the
    published App Kit contract; loop-native callers use the ``*_async``
    siblings). These unit tests exercise them against a mock service on a
    loop-less thread, so ``sdk.add_job(...)`` already returns its result
    directly — this wrapper simply returns it (kept so call sites read
    uniformly and any raised ``ValueError`` still surfaces from the argument
    evaluation).
    """
    return value

# ---------------------------------------------------------------------------
# Mock CronService and CronJob
# ---------------------------------------------------------------------------


@dataclass
class MockCronJob:
    id: str = ""
    name: str = ""
    message: str = ""
    created_by: str = ""
    agent_id: str = ""
    command: str = ""
    script: str = ""
    agent_sequence: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    persistent_session: bool = True
    silent: bool = False
    enabled: bool = True
    user_paused: bool = False
    every_secs: int | None = None
    cron_expr: str | None = None
    timezone: str = ""
    skip_dates: list[str] = field(default_factory=list)
    folder_id: str = ""
    # Mirrors CronJob: "" is hook-based approval, 1800 is _JOB_TIMEOUT_SECS (the
    # per-wake budget is never stored as 0), and 0 means the per-kind subprocess
    # default (30s script / 300s command).
    approval_mode: str = ""
    timeout_secs: int = 1800
    timeout: int = 0


class MockCronService:
    def __init__(self) -> None:
        self._jobs: list[MockCronJob] = []
        self._next_id = 1
        # Claim bookkeeping for the manual-run surface: job id -> attached task
        # (None until attach_run_task hands one in), matching CronService's
        # "membership means the job is running" reading of its own claims.
        self._claims: dict[str, Any] = {}
        self.runs: list[str] = []
        self.run_duration = 0.0
        self.run_raises: BaseException | None = None

    def add_job(self, **kwargs: Any) -> MockCronJob:
        # Mirror CronService.add_job / _build_job: enabled=False creates the job
        # already paused (user_paused=True), and the mutable list/dict fields
        # are normalized to concrete empties (never None).
        kwargs["agent_sequence"] = list(kwargs.get("agent_sequence") or [])
        kwargs["env"] = dict(kwargs.get("env") or {})
        kwargs["skip_dates"] = list(kwargs.get("skip_dates") or [])
        kwargs["timezone"] = kwargs.get("timezone") or ""
        # _build_job stores the per-wake default rather than 0 when the field is
        # unset, so a test asserting "default budget" sees the same number here
        # as it would on a real job.
        kwargs["timeout_secs"] = int(kwargs.get("timeout_secs") or 0) or 1800
        job = MockCronJob(
            id=f"job-{self._next_id}",
            user_paused=not kwargs.get("enabled", True),
            **kwargs,
        )
        self._next_id += 1
        self._jobs.append(job)
        return job

    async def add_job_async(self, **kwargs: Any) -> MockCronJob:
        return self.add_job(**kwargs)

    async def add_job_if_absent_async(
        self, predicate: Any, **kwargs: Any
    ) -> MockCronJob | None:
        if any(predicate(j) for j in self._jobs):
            return None
        return self.add_job(**kwargs)

    def list_jobs(self, include_disabled: bool = False) -> list[MockCronJob]:
        if include_disabled:
            return list(self._jobs)
        return [j for j in self._jobs if j.enabled]

    def remove_job(self, job_id: str, *, actor: str, source: str) -> bool:
        for i, j in enumerate(self._jobs):
            if j.id == job_id:
                self._jobs.pop(i)
                return True
        return False

    async def remove_job_async(self, job_id: str, *, actor: str, source: str) -> bool:
        return self.remove_job(job_id, actor=actor, source=source)

    def remove_jobs_sync(
        self, job_ids: list[str], *, actor: str, source: str
    ) -> tuple[list[str], list[str]]:
        removed: list[str] = []
        missing: list[str] = []
        present = {j.id for j in self._jobs}
        targets = {jid for jid in job_ids if jid in present}
        for jid in job_ids:
            (removed if jid in present else missing).append(jid)
        if targets:
            self._jobs = [j for j in self._jobs if j.id not in targets]
        return removed, missing

    async def remove_jobs(
        self, job_ids: list[str], *, actor: str, source: str
    ) -> tuple[list[str], list[str]]:
        return self.remove_jobs_sync(list(job_ids), actor=actor, source=source)

    def remove_jobs_by_owner_sync(self, owner_prefix: str) -> list[str]:
        removed = [
            j.id for j in self._jobs
            if getattr(j, "created_by", "") == owner_prefix
        ]
        if removed:
            targets = set(removed)
            self._jobs = [j for j in self._jobs if j.id not in targets]
        return removed

    async def remove_jobs_by_owner(self, owner_prefix: str) -> list[str]:
        return self.remove_jobs_by_owner_sync(owner_prefix)

    def update_job(self, job_id: str, **kwargs: Any) -> MockCronJob | None:
        for j in self._jobs:
            if j.id == job_id:
                for k, v in kwargs.items():
                    setattr(j, k, v)
                return j
        return None

    async def update_job_async(self, job_id: str, **kwargs: Any) -> MockCronJob | None:
        return self.update_job(job_id, **kwargs)

    # ── Manual-run surface (mirrors CronService's claim bookkeeping) ──
    #
    # run_job is a plain ``def`` returning a coroutine, exactly as CronService
    # declares it: the claim is taken while the call expression is evaluated,
    # before the caller's create_task has scheduled anything. A mock that made it
    # ``async def`` would move the claim past the first await and stop testing
    # the atomicity the real guard depends on.

    def is_running(self, job_id: str) -> bool:
        return job_id in self._claims

    def discard_finished_run(self, job_id: str) -> bool:
        task = self._claims.get(job_id)
        if task is not None and task.done():
            del self._claims[job_id]
            return True
        return False

    def attach_run_task(self, job_id: str, task: Any) -> None:
        if job_id in self._claims and self._claims[job_id] is None:
            self._claims[job_id] = task

    # The REAL check-and-claim section, run against this mock's claim
    # primitives, so the SDK tests exercise the one shared copy rather than a
    # stand-in that could drift from it.
    trigger_run = CronService.trigger_run
    _push_refresh = None

    def run_job(
        self,
        job_id: str,
        *,
        expected_owner: str | None = None,
        started: asyncio.Future[bool] | None = None,
    ) -> Any:
        if job_id in self._claims:
            return self._refused()
        self._claims[job_id] = None
        self.runs.append(job_id)
        return self._run_claimed(job_id, started)

    async def _refused(self) -> bool:
        return False

    async def _run_claimed(
        self, job_id: str, started: asyncio.Future[bool] | None = None
    ) -> bool:
        # The mock's run always spawns, so it reports a start at once, as the
        # real service does right after it spawns the run; a run_raises failure
        # is then a failure of a started run.
        if started is not None and not started.done():
            started.set_result(True)
        if self.run_raises is not None:
            self._claims.pop(job_id, None)
            raise self.run_raises
        await asyncio.sleep(self.run_duration)
        self._claims.pop(job_id, None)
        return True


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

def _app_name() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z0-9-]{2,12}", fullmatch=True)


def _job_name() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z0-9 -]{2,20}", fullmatch=True)


def _agent_sequence() -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.from_regex(r"[a-z][a-z0-9-]{2,15}", fullmatch=True),
        max_size=4,
    )


def _env_dict() -> st.SearchStrategy[dict[str, str]]:
    key = st.from_regex(r"[A-Z][A-Z0-9_]{1,10}", fullmatch=True)
    val = st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N")))
    return st.dictionaries(key, val, max_size=3)


# ---------------------------------------------------------------------------
# Property 3: Cron job creation preserves ownership and fields
# ---------------------------------------------------------------------------


class TestCronJobCreation:
    """Property 3: Cron job creation preserves ownership and fields.

    **Validates: Requirements 2.1, 2.7**
    """

    @settings(max_examples=100)
    @given(
        app_name=_app_name(),
        job_name=_job_name(),
        agent_seq=_agent_sequence(),
        env=_env_dict(),
        persistent=st.booleans(),
        silent=st.booleans(),
    )
    def test_job_creation_preserves_fields(
        self, app_name: str, job_name: str, agent_seq: list[str],
        env: dict[str, str], persistent: bool, silent: bool,
    ) -> None:
        """Created job has correct ownership and all fields match input."""
        svc = MockCronService()
        sdk = CronSDK(app_name, svc)

        job = _run(sdk.add_job(
            name=job_name,
            message="test",
            cron_expr="* * * * *",
            agent_sequence=agent_seq,
            env=env,
            persistent_session=persistent,
            silent=silent,
        ))

        assert job.created_by == f"app:{app_name}"
        assert job.agent_sequence == agent_seq
        assert job.env == env
        assert job.persistent_session == persistent
        assert job.silent == silent

    def test_disabled_job_registers_paused(self) -> None:
        """enabled=False creates the job in a paused, user-resumable state."""
        svc = MockCronService()
        sdk = CronSDK("my-app", svc)

        job = _run(sdk.add_job(
            name="my-app/nightly-run",
            message="",
            cron_expr="0 22 * * *",
            enabled=False,
        ))

        assert job.enabled is False
        assert job.user_paused is True

    def test_enabled_default_registers_active(self) -> None:
        """Default add_job (no enabled kwarg) creates an active job."""
        svc = MockCronService()
        sdk = CronSDK("my-app", svc)

        job = _run(sdk.add_job(name="my-app/refresh", message="go", cron_expr="* * * * *"))

        assert job.enabled is True
        assert getattr(job, "user_paused", False) is False

    def test_paused_at_registration_job_can_be_resumed(self, tmp_path: Path) -> None:
        """A real persisted disabled job resumes through the owned toggle API."""
        svc = CronService(base_dir=tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("my-app", svc)

        job = _run(sdk.add_job(
            name="my-app/nightly-run",
            message="",
            cron_expr="0 22 * * *",
            enabled=False,
        ))
        assert job.enabled is False

        assert sdk.set_enabled(job.id, True)
        updated = sdk.list_jobs()[0]
        assert updated.id == job.id
        assert updated.enabled is True
        assert updated.user_paused is False
        # Resumed job shows up in the active (non-disabled) list again.
        assert updated in svc.list_jobs()


# ---------------------------------------------------------------------------
# Calendar fields (timezone / skip_dates) are settable AT CREATE
# ---------------------------------------------------------------------------


class TestCronCalendarFieldsOnCreate:
    """``timezone``/``skip_dates`` reach ``CronService.add_job`` from the SDK.

    The gap this closes: ``_add_job_kwargs`` was a closed allowlist that
    omitted both fields, so an app could only ever create jobs with an empty
    timezone -- resolving to UTC at fire time -- and had to issue a SECOND
    ``update_job`` write to correct it. That second write is exactly the
    half-formed-job window the single locked build+persist exists to remove: a
    "run at 06:00 local" job briefly exists as 06:00 UTC.
    """

    def test_add_job_threads_timezone_and_skip_dates(self) -> None:
        """The sync create path persists both calendar fields on the job."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = _run(sdk.add_job(
            name="digest-app/daily",
            message="summarise the last 24 hours",
            cron_expr="0 6 * * *",
            timezone="America/Los_Angeles",
            skip_dates=["2026-12-25"],
        ))

        assert job.timezone == "America/Los_Angeles"
        assert job.skip_dates == ["2026-12-25"]

    def test_add_job_defaults_leave_calendar_fields_empty(self) -> None:
        """Omitting both keeps today's behaviour (config timezone, then UTC)."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = _run(sdk.add_job(
            name="digest-app/daily", message="go", cron_expr="0 6 * * *",
        ))

        assert job.timezone == ""
        assert job.skip_dates == []

    @pytest.mark.asyncio
    async def test_add_job_async_threads_timezone_and_skip_dates(self) -> None:
        """The loop-native create path threads both fields too."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = await sdk.add_job_async(
            name="digest-app/daily",
            message="go",
            cron_expr="0 6 * * *",
            timezone="Australia/Sydney",
            skip_dates=["2026-01-01", "2026-01-26"],
        )

        assert job.timezone == "Australia/Sydney"
        assert job.skip_dates == ["2026-01-01", "2026-01-26"]

    @pytest.mark.asyncio
    async def test_add_job_if_absent_async_threads_timezone(self) -> None:
        """The atomic add-if-absent path (app-manifest registration) too."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = await sdk.add_job_if_absent_async(
            name="digest-app/daily",
            message="go",
            cron_expr="0 6 * * *",
            timezone="Europe/Berlin",
        )

        assert job is not None
        assert job.timezone == "Europe/Berlin"


class TestCronCalendarFieldsAgainstRealService:
    """End-to-end against the real ``CronService``: one locked save, validated.

    The mock service above proves the SDK forwards the kwargs; these prove the
    real persistence owner accepts them at create, writes them in the job's
    FIRST and only save, and rejects invalid values before anything lands on
    disk.
    """

    def _service(self, tmp_path: Path) -> CronService:
        svc = CronService(base_dir=tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        return svc

    def test_timezone_lands_in_the_first_persisted_write(self, tmp_path: Path) -> None:
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        job = sdk.add_job(
            name="digest-app/daily",
            message="summarise the last 24 hours",
            cron_expr="0 6 * * *",
            timezone="America/Los_Angeles",
            skip_dates=["2026-12-25"],
        )

        assert job.timezone == "America/Los_Angeles"
        assert job.skip_dates == ["2026-12-25"]
        # The store on disk carries them, so no follow-up update_job is needed
        # and the job is never observable with the wrong timezone.
        on_disk = json.loads(svc._path.read_text())
        entry = next(j for j in on_disk["jobs"] if j["id"] == job.id)
        assert entry["timezone"] == "America/Los_Angeles"
        assert entry["skip_dates"] == ["2026-12-25"]
        assert entry["created_by"] == "app:digest-app"

    def test_unknown_timezone_raises_and_persists_nothing(self, tmp_path: Path) -> None:
        """An app author learns at create time, not at fire time."""
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        with pytest.raises(ValueError, match="Invalid timezone"):
            sdk.add_job(
                name="digest-app/daily",
                message="go",
                cron_expr="0 6 * * *",
                timezone="Mars/Olympus_Mons",
            )

        assert svc.list_jobs(include_disabled=True) == []

    def test_malformed_skip_date_raises_and_persists_nothing(
        self, tmp_path: Path
    ) -> None:
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        with pytest.raises(ValueError, match="Invalid skip_date"):
            sdk.add_job(
                name="digest-app/daily",
                message="go",
                cron_expr="0 6 * * *",
                skip_dates=["25/12/2026"],
            )

        assert svc.list_jobs(include_disabled=True) == []


class TestCronApprovalAndTimeoutFieldsOnCreate:
    """``approval_mode``/``timeout_secs``/``timeout`` reach the service from the SDK.

    The gap this closes is the same shape as the calendar-field gap above:
    ``_add_job_kwargs`` was a closed allowlist, so passing any of these three
    to an ``add_job*`` method was a ``TypeError`` and an app could only get
    them onto a job with a SECOND ``update_job`` write.

    For ``approval_mode`` that second write is not merely inelegant. The job
    exists on disk with hook-based approval until it lands, so a due-scan in
    that window runs the job under a mode the app did not ask for, and an
    unattended agent job stalls on a prompt nobody answers.
    """

    def test_add_job_threads_approval_mode(self) -> None:
        """The sync create path persists the mode on the job."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = _run(sdk.add_job(
            name="digest-app/unattended",
            message="summarise the last 24 hours",
            every_secs=3600,
            approval_mode="auto",
        ))

        assert job.approval_mode == "auto"

    def test_add_job_defaults_leave_hook_based_approval(self) -> None:
        """Omitting it keeps today's behaviour: hook-based approval."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = _run(sdk.add_job(
            name="digest-app/attended", message="go", every_secs=3600,
        ))

        assert job.approval_mode == ""

    @pytest.mark.asyncio
    async def test_add_job_async_threads_approval_mode(self) -> None:
        """The loop-native path too -- the one an app hook actually awaits."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = await sdk.add_job_async(
            name="digest-app/unattended",
            message="go",
            every_secs=3600,
            approval_mode="auto",
        )

        assert job.approval_mode == "auto"

    @pytest.mark.asyncio
    async def test_add_job_if_absent_async_threads_approval_mode(self) -> None:
        """The atomic add-if-absent path, which is what ``bridges`` calls.

        This one carries the most weight: it returns None once the name is
        present, so an app correcting the mode in a follow-up ``update_job``
        would skip that correction on every registration after the first.
        """
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = await sdk.add_job_if_absent_async(
            name="digest-app/unattended",
            message="go",
            every_secs=3600,
            approval_mode="auto",
        )

        assert job is not None
        assert job.approval_mode == "auto"

    def test_add_job_threads_the_timeout_pair(self) -> None:
        """Both budgets reach the service from one create call."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = _run(sdk.add_job(
            name="digest-app/probe",
            message="poll the queue",
            every_secs=3600,
            command="/bin/true",
            timeout_secs=120,
            timeout=60,
        ))

        assert job.timeout_secs == 120
        assert job.timeout == 60

    def test_add_job_defaults_leave_the_budgets_at_their_defaults(self) -> None:
        """Omitting both keeps the per-wake default and the per-kind default."""
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = _run(sdk.add_job(
            name="digest-app/probe", message="go", every_secs=3600,
        ))

        assert job.timeout_secs == 1800
        assert job.timeout == 0

    @pytest.mark.asyncio
    async def test_add_job_async_threads_the_timeout_pair(self) -> None:
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = await sdk.add_job_async(
            name="digest-app/probe",
            message="go",
            every_secs=3600,
            command="/bin/true",
            timeout_secs=120,
            timeout=60,
        )

        assert job.timeout_secs == 120
        assert job.timeout == 60

    @pytest.mark.asyncio
    async def test_add_job_if_absent_async_threads_the_timeout_pair(self) -> None:
        svc = MockCronService()
        sdk = CronSDK("digest-app", svc)

        job = await sdk.add_job_if_absent_async(
            name="digest-app/probe",
            message="go",
            every_secs=3600,
            command="/bin/true",
            timeout_secs=120,
            timeout=60,
        )

        assert job is not None
        assert job.timeout_secs == 120
        assert job.timeout == 60


class TestCronApprovalAndTimeoutFieldsAgainstRealService:
    """End-to-end against the real ``CronService``: one locked save, validated.

    The mock class above proves the SDK forwards the kwargs; these prove the
    real persistence owner accepts them at create, writes them in the job's
    FIRST and only save, and refuses invalid values before anything reaches
    disk.
    """

    def _service(self, tmp_path: Path) -> CronService:
        svc = CronService(base_dir=tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        return svc

    def test_approval_mode_lands_in_the_first_persisted_write(
        self, tmp_path: Path
    ) -> None:
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        job = sdk.add_job(
            name="digest-app/unattended",
            message="summarise the last 24 hours",
            every_secs=3600,
            approval_mode="auto",
        )

        assert job.approval_mode == "auto"
        # On disk after the single locked save, so the job is never observable
        # with hook-based approval and no follow-up update_job is needed.
        on_disk = json.loads(svc._path.read_text())
        entry = next(j for j in on_disk["jobs"] if j["id"] == job.id)
        assert entry["approval_mode"] == "auto"
        assert entry["created_by"] == "app:digest-app"

    def test_invalid_approval_mode_raises_and_persists_nothing(
        self, tmp_path: Path
    ) -> None:
        """An app author learns at create time, not at fire time."""
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        with pytest.raises(ValueError, match="Invalid approval_mode"):
            sdk.add_job(
                name="digest-app/unattended",
                message="go",
                every_secs=3600,
                approval_mode="yolo",
            )

        assert svc.list_jobs(include_disabled=True) == []

    def test_the_timeout_pair_lands_in_the_first_persisted_write(
        self, tmp_path: Path
    ) -> None:
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        job = sdk.add_job(
            name="digest-app/probe",
            message="poll the queue",
            every_secs=3600,
            command="/bin/true",
            timeout_secs=120,
            timeout=60,
        )

        on_disk = json.loads(svc._path.read_text())
        entry = next(j for j in on_disk["jobs"] if j["id"] == job.id)
        assert entry["timeout_secs"] == 120
        assert entry["timeout"] == 60

    def test_out_of_range_wake_budget_raises_and_persists_nothing(
        self, tmp_path: Path
    ) -> None:
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        with pytest.raises(ValueError, match="timeout_secs must be within"):
            sdk.add_job(
                name="digest-app/probe",
                message="go",
                every_secs=3600,
                timeout_secs=99999,
            )

        assert svc.list_jobs(include_disabled=True) == []

    def test_a_short_wake_budget_needs_its_subprocess_timeout(
        self, tmp_path: Path
    ) -> None:
        """Why ``timeout`` is threaded alongside ``timeout_secs``.

        ``build_job`` cross-checks the wake budget against the subprocess
        timeout, falling back to 300s for a command when that is unset. So on a
        command job a short budget is REFUSED unless the pair is set together,
        and threading only ``timeout_secs`` would leave an app unable to create
        a command job with a budget under 305s at all. Both arms are asserted:
        a test that only showed the refusal would also pass if the create path
        rejected every budget.
        """
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)

        with pytest.raises(ValueError, match="must cover the command/script"):
            sdk.add_job(
                name="digest-app/probe-refused",
                message="go",
                every_secs=3600,
                command="/bin/true",
                timeout_secs=60,
            )
        assert svc.list_jobs(include_disabled=True) == []

        job = sdk.add_job(
            name="digest-app/probe-accepted",
            message="go",
            every_secs=3600,
            command="/bin/true",
            timeout_secs=60,
            timeout=30,
        )
        assert job.timeout_secs == 60
        assert job.timeout == 30

    def test_an_auto_mode_job_runs_without_an_approval_prompt(
        self, tmp_path: Path
    ) -> None:
        """The reason an app wants this field, measured rather than asserted.

        Takes the job the SDK actually persisted and feeds it to the real
        gateway cron callback, then reads what that callback hands the agent
        stream. ``auto`` must yield AUTO_APPROVE with no approval callback --
        that absent callback is what "unattended" means. Both modes are
        checked, because the auto arm alone would also pass on a callback that
        never installed an approval hook for anything.
        """
        svc = self._service(tmp_path)
        sdk = CronSDK("digest-app", svc)
        unattended = sdk.add_job(
            name="digest-app/unattended", message="go",
            every_secs=3600, approval_mode="auto",
        )
        attended = sdk.add_job(
            name="digest-app/attended", message="go", every_secs=3600,
        )

        def _dispatch(job: Any) -> dict[str, Any]:
            # Local import: pulling the gateway module in at collection time
            # would cost every other test in this file, which needs none of it.
            # Same placement as test_cron_approval_mode.py's own harness.
            from kiro_crew.slack.gateway import GatewayOrchestrator

            gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
            gw.sessions = MagicMock()
            gw.sessions.get_pid = MagicMock(return_value=None)
            gw.ctx_builder = MagicMock()
            gw.ctx_builder.conversation_log.get_metadata_status.return_value = ({}, True)
            gw.slack = MagicMock()
            gw.conv_log = None
            gw.dashboard_state = None
            gw._owner_id = "U000"
            gw.subagent_mgr = None
            gw._cron_injecting = {}
            gw._no_crons = False
            gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
            gw.sessions.release = MagicMock()
            gw.sessions.reset = AsyncMock()
            gw.sessions.cancel_current = AsyncMock()
            gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
            gw.ctx_builder.hooks = MagicMock()
            gw._interactive_approval = MagicMock(return_value="interactive_cb")

            captured: dict[str, Any] = {}

            async def fake_stream(client: Any, msg: Any, **kwargs: Any) -> str:
                captured.update(kwargs)
                return "done"

            captured_cb: Any = None

            # The fire-time vet refuses a job whose owning app is not installed
            # and enabled, which no temp-dir fixture can satisfy. That gate is a
            # different invariant with its own coverage; neutralising it here is
            # what lets this test measure the approval decision rather than
            # re-measure the gate.
            with patch("kiro_crew.slack.gateway.stream_and_collect", fake_stream), patch(
                "kiro_crew.slack.gateway.vet_job_at_fire_time", lambda _job: None
            ), patch(
                "kiro_crew.slack.gateway.CronService"
            ) as mock_cron_cls:

                def capture_cron(on_job: Any = None, **kw: Any) -> Any:
                    nonlocal captured_cb
                    captured_cb = on_job
                    stub = MagicMock()
                    stub.start = AsyncMock()
                    return stub

                mock_cron_cls.create = AsyncMock(side_effect=capture_cron)

                async def _init_and_run() -> None:
                    await gw._init_cron()
                    assert captured_cb is not None
                    await captured_cb(job)

                asyncio.run(_init_and_run())

            return captured

        auto = _dispatch(unattended)
        assert auto["approval_policy"] == ToolApprovalPolicy.AUTO_APPROVE
        assert auto["on_tool_approval"] is None

        hook = _dispatch(attended)
        assert hook["approval_policy"] == ToolApprovalPolicy.HOOK_BASED
        assert hook["on_tool_approval"] is not None


# ---------------------------------------------------------------------------
# Property 4: Cron ownership enforcement on mutations
# ---------------------------------------------------------------------------


class TestCronOwnershipEnforcement:
    """Property 4: Cron ownership enforcement on mutations.

    **Validates: Requirements 2.2, 2.4, 2.5**
    """

    @settings(max_examples=100)
    @given(app_a=_app_name(), app_b=_app_name())
    def test_cross_app_remove_raises(self, app_a: str, app_b: str) -> None:
        """Removing a job owned by app A from app B's SDK raises PermissionError."""
        if app_a == app_b:
            return  # skip trivial case

        svc = MockCronService()
        sdk_a = CronSDK(app_a, svc)
        sdk_b = CronSDK(app_b, svc)

        job = _run(sdk_a.add_job(name="test-job", message="msg", cron_expr="* * * * *"))

        with pytest.raises(PermissionError):
            _run(sdk_b.remove_job(job.id))

        # Job still exists
        assert len(sdk_a.list_jobs()) == 1

    @settings(max_examples=100)
    @given(app_a=_app_name(), app_b=_app_name())
    def test_cross_app_update_raises(self, app_a: str, app_b: str) -> None:
        """Updating a job owned by app A from app B's SDK raises PermissionError."""
        if app_a == app_b:
            return

        svc = MockCronService()
        sdk_a = CronSDK(app_a, svc)
        sdk_b = CronSDK(app_b, svc)

        job = _run(sdk_a.add_job(name="test-job", message="msg", cron_expr="* * * * *"))

        with pytest.raises(PermissionError):
            _run(sdk_b.update_job(job.id, message="hacked"))

        # Job unchanged
        assert svc._jobs[0].message == "msg"


# ---------------------------------------------------------------------------
# Property 5: Cron list filtering by owner
# ---------------------------------------------------------------------------


class TestCronListFiltering:
    """Property 5: Cron list filtering by owner.

    **Validates: Requirements 2.3**
    """

    @settings(max_examples=100)
    @given(
        app_a=_app_name(),
        app_b=_app_name(),
        n_a=st.integers(min_value=0, max_value=5),
        n_b=st.integers(min_value=0, max_value=5),
    )
    def test_list_returns_only_owned_jobs(
        self, app_a: str, app_b: str, n_a: int, n_b: int,
    ) -> None:
        """list_jobs() returns exactly the jobs owned by the calling app."""
        if app_a == app_b:
            return

        svc = MockCronService()
        sdk_a = CronSDK(app_a, svc)
        sdk_b = CronSDK(app_b, svc)

        for i in range(n_a):
            _run(sdk_a.add_job(name=f"a-job-{i}", message="a", cron_expr="* * * * *"))
        for i in range(n_b):
            _run(sdk_b.add_job(name=f"b-job-{i}", message="b", cron_expr="* * * * *"))

        assert len(sdk_a.list_jobs()) == n_a
        assert len(sdk_b.list_jobs()) == n_b


# ---------------------------------------------------------------------------
# Property 6: Cron remove_all completeness
# ---------------------------------------------------------------------------


class TestCronRemoveAll:
    """Property 6: Cron remove_all completeness.

    **Validates: Requirements 2.6**
    """

    @settings(max_examples=100)
    @given(app_name=_app_name(), n_jobs=st.integers(min_value=1, max_value=10))
    def test_remove_all_clears_owned_jobs(self, app_name: str, n_jobs: int) -> None:
        """After remove_all(), list_jobs() returns empty for that app."""
        svc = MockCronService()
        sdk = CronSDK(app_name, svc)

        for i in range(n_jobs):
            _run(sdk.add_job(name=f"job-{i}", message="msg", cron_expr="* * * * *"))

        assert len(sdk.list_jobs()) == n_jobs
        removed = _run(sdk.remove_all())
        assert removed == n_jobs
        assert len(sdk.list_jobs()) == 0

    @settings(max_examples=50)
    @given(app_a=_app_name(), app_b=_app_name())
    def test_remove_all_does_not_affect_other_apps(self, app_a: str, app_b: str) -> None:
        """remove_all() for app A does not remove app B's jobs."""
        if app_a == app_b:
            return

        svc = MockCronService()
        sdk_a = CronSDK(app_a, svc)
        sdk_b = CronSDK(app_b, svc)

        _run(sdk_a.add_job(name="a-job", message="a", cron_expr="* * * * *"))
        _run(sdk_b.add_job(name="b-job", message="b", cron_expr="* * * * *"))

        _run(sdk_a.remove_all())
        assert len(sdk_a.list_jobs()) == 0
        assert len(sdk_b.list_jobs()) == 1


# ---------------------------------------------------------------------------
# Storage-layer command/script vetting (deny-by-default)
# ---------------------------------------------------------------------------


class TestCronVettingDenyPath:
    """add_job() vets command/script BEFORE creating the job (deny-by-default).

    Covers the storage-layer defense-in-depth added to ``CronSDK.add_job``: a
    rejected command/script must raise ``ValueError`` and must NOT land a job in
    the cron service (no zombie job on rejection).
    """

    def test_add_job_rejects_malicious_command(self) -> None:
        """A command blocked by _vet_shell_command raises and creates no job."""
        svc = MockCronService()
        sdk = CronSDK("evil-app", svc)

        with pytest.raises(ValueError, match="cron command rejected"):
            _run(sdk.add_job(
                name="exfil",
                message="",
                command="cat ~/.aws/credentials",
                cron_expr="* * * * *",
            ))

        # Deny-by-default: nothing was added to the service.
        assert svc._jobs == []

    def test_add_job_rejects_malicious_script(self, tmp_path, monkeypatch) -> None:
        """A script whose body fails vetting raises and creates no job.

        Uses a real script under the sanctioned ``~/.kirocrew/crons/`` dir (so
        ``resolve_script_path`` succeeds) whose body references a credential
        path, so ``_vet_script_file`` returns an error and add_job hits the
        script deny branch.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        crons_dir = tmp_path / ".kirocrew" / "crons"
        crons_dir.mkdir(parents=True)
        evil = crons_dir / "evil.py"
        evil.write_text(
            "import os\n"
            "def run(ctx):\n"
            "    # exfiltrate the caller's AWS creds\n"
            "    return open(os.path.expanduser('~/.aws/credentials')).read()\n"
        )

        svc = MockCronService()
        sdk = CronSDK("evil-app", svc)

        with pytest.raises(ValueError, match="cron script rejected"):
            _run(sdk.add_job(
                name="exfil",
                message="",
                script="~/.kirocrew/crons/evil.py:run",
                cron_expr="* * * * *",
            ))

        # Deny-by-default: nothing was added to the service.
        assert svc._jobs == []

    def test_add_job_rejects_script_outside_sanctioned_dir(
        self, tmp_path, monkeypatch
    ) -> None:
        """A script path outside ~/.kirocrew/crons/ is denied (resolve raises).

        ``resolve_script_path`` raises ``PermissionError``/``FileNotFoundError``
        for paths outside the sanctioned dir; add_job must convert that into a
        ``ValueError`` and emit a SEL denied audit rather than letting it
        propagate unaudited.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".kirocrew" / "crons").mkdir(parents=True)

        svc = MockCronService()
        sdk = CronSDK("evil-app", svc)

        with pytest.raises(ValueError, match="cron script rejected"):
            _run(sdk.add_job(
                name="escape",
                message="",
                script="/etc/passwd:run",
                cron_expr="* * * * *",
            ))

        # Deny-by-default: nothing was added to the service.
        assert svc._jobs == []


class TestOwnedCronToggle:
    """Exercise real persistence, including a cache that missed an owner change."""

    @pytest.fixture
    def service(self, tmp_path):
        svc = CronService(base_dir=tmp_path)
        svc._dir.mkdir(parents=True, exist_ok=True)
        return svc

    def test_toggle_preserves_id_and_fields(self, service):
        sdk = CronSDK("example", service)
        job = sdk.add_job(name="example/daily", message="hello", every_secs=600)
        assert sdk.set_enabled(job.id, False) is True
        saved = json.loads(service._path.read_text(encoding="utf-8"))["jobs"][0]
        assert (saved["id"], saved["enabled"], saved["user_paused"]) == (job.id, False, True)
        assert saved["message"] == "hello"
        assert sdk.set_enabled(job.id, True) is True
        saved = json.loads(service._path.read_text(encoding="utf-8"))["jobs"][0]
        assert (saved["id"], saved["enabled"], saved["user_paused"]) == (job.id, True, False)

    def test_foreign_and_missing_ids_refuse_and_audit(self, service, monkeypatch):
        from unittest.mock import Mock

        audit = Mock()
        monkeypatch.setattr("kiro_crew.apps.cron_sdk.sel", lambda: audit)
        other = CronSDK("other", service).add_job(name="other/job", message="hello", every_secs=600)
        sdk = CronSDK("example", service)
        before = service._path.read_bytes()
        for job_id in [other.id, "missing"]:
            with pytest.raises(PermissionError):
                sdk.set_enabled(job_id, False)
            assert audit.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert service._path.read_bytes() == before

    def test_owner_is_rechecked_after_store_reload(self, service):
        sdk = CronSDK("example", service)
        job = sdk.add_job(name="example/job", message="hello", every_secs=600)
        state = json.loads(service._path.read_text(encoding="utf-8"))
        state["jobs"][0]["created_by"] = "app:other"
        service._path.write_text(json.dumps(state), encoding="utf-8")
        before = service._path.read_bytes()
        with pytest.raises(PermissionError):
            sdk.set_enabled(job.id, False)
        assert service._path.read_bytes() == before

    @pytest.mark.asyncio
    async def test_async_toggle_and_sync_loop_refusal(self, service):
        from kiro_crew.apps.cron_sdk import CronSyncOnLoopError

        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/job", message="hello", every_secs=600)
        with pytest.raises(CronSyncOnLoopError, match="set_enabled_async"):
            sdk.set_enabled(job.id, False)
        assert await sdk.set_enabled_async(job.id, False)
        import asyncio

        saved = await asyncio.to_thread(service._path.read_text, encoding="utf-8")
        assert json.loads(saved)["jobs"][0]["enabled"] is False
        with pytest.raises(PermissionError):
            await CronSDK("other", service).set_enabled_async(job.id, True)

    @pytest.mark.parametrize("field,value", [("enabled", False), ("user_paused", True)])
    def test_pause_update_is_not_silently_ignored(self, service, field, value):
        sdk = CronSDK("example", service)
        job = sdk.add_job(name="example/job", message="hello", every_secs=600)
        before = service._path.read_bytes()
        with pytest.raises(ValueError, match="set_enabled"):
            sdk.update_job(job.id, message="changed", **{field: value})
        assert service._path.read_bytes() == before
        assert job.message == "hello"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field,value", [("enabled", False), ("user_paused", True)])
    async def test_async_pause_update_is_not_silently_ignored(self, service, field, value):
        import asyncio

        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/job", message="hello", every_secs=600)
        before = await asyncio.to_thread(service._path.read_bytes)
        with pytest.raises(ValueError, match="set_enabled"):
            await sdk.update_job_async(job.id, message="changed", **{field: value})
        assert await asyncio.to_thread(service._path.read_bytes) == before
        assert job.message == "hello"

    @pytest.mark.parametrize("value", ["false", 0, 1, None])
    def test_toggle_requires_boolean(self, service, value):
        with pytest.raises(ValueError, match="boolean"):
            CronSDK("example", service).set_enabled("missing", value)


class TestOwnedCronManualRun:
    """``run_job_async``: run an owned job off schedule, without mutating it.

    The verb exists because an app observes work is ready in its OWN loop, not
    on the job's schedule, and the only lever before it was lowering
    ``every_secs`` until the next-due calculation landed in the past -- which
    persists a transient intent and is floored at 60s.
    """

    @pytest.fixture
    def svc(self) -> MockCronService:
        return MockCronService()

    @pytest.fixture
    def sdk(self, svc: MockCronService) -> CronSDK:
        return CronSDK("example", svc)

    async def _owned(self, sdk: CronSDK, **kwargs: Any) -> MockCronJob:
        """Create the owned job through the async form: these tests run ON the
        loop, where the synchronous ``add_job`` is refused by design."""
        return await sdk.add_job_async(
            name="example/poll", message="go", every_secs=3600, **kwargs
        )

    @pytest.mark.asyncio
    async def test_an_owned_job_is_dispatched(self, svc, sdk):
        """A run is claimed and started, and the call reports the dispatch."""
        job = await self._owned(sdk)
        assert await sdk.run_job_async(job.id) is True
        assert svc.runs == [job.id]

    @pytest.mark.asyncio
    async def test_the_schedule_is_not_mutated_by_a_run(self, svc, sdk):
        """The whole point of the verb: a one-off run leaves the schedule alone.

        A fast-forward implemented by lowering ``every_secs`` would show up here
        as a changed interval or an ``update_job`` call, which is what this
        pins against.
        """
        job = await self._owned(sdk)
        before = (job.every_secs, job.cron_expr, job.enabled, job.user_paused)
        await sdk.run_job_async(job.id)
        assert (job.every_secs, job.cron_expr, job.enabled, job.user_paused) == before

    @pytest.mark.asyncio
    async def test_a_second_run_is_refused_while_one_is_in_flight(self, svc, sdk):
        """An overlapping run is refused rather than orphaning the first task."""
        job = await self._owned(sdk)
        svc.run_duration = 5.0
        first = asyncio.create_task(sdk.run_job_async(job.id))
        await asyncio.sleep(0)  # let the first dispatch take the claim
        assert await sdk.run_job_async(job.id) is False
        assert svc.runs == [job.id]  # the refusal never reached run_job
        assert await first is True
        svc._claims.clear()

    @pytest.mark.asyncio
    async def test_a_finished_run_does_not_block_the_next_one(self, svc, sdk):
        """A claim left behind by an already-finished task is dropped, not obeyed.

        Without the ``discard_finished_run`` call the guard alone would refuse
        every later run until the reaper sweep met the finished task.
        """
        job = await self._owned(sdk)
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        done.set_result(True)
        svc._claims[job.id] = done  # a stale claim tracking a finished task
        assert await sdk.run_job_async(job.id) is True
        assert svc.runs == [job.id]

    @pytest.mark.asyncio
    async def test_foreign_and_missing_ids_refuse_and_audit(self, svc, monkeypatch):
        """Ownership is enforced before anything is claimed or dispatched."""
        from unittest.mock import Mock

        audit = Mock()
        monkeypatch.setattr("kiro_crew.apps.cron_sdk.sel", lambda: audit)
        other = await CronSDK("other", svc).add_job_async(
            name="other/poll", message="go", every_secs=3600
        )
        sdk = CronSDK("example", svc)
        for job_id in [other.id, "missing"]:
            with pytest.raises(PermissionError):
                await sdk.run_job_async(job_id)
            kwargs = audit.log_api_access.call_args.kwargs
            assert (kwargs["operation"], kwargs["outcome"]) == ("cron_run_job", "denied")
        assert svc.runs == []

    @pytest.mark.asyncio
    async def test_dispatch_and_refusal_are_both_audited(self, svc, sdk, monkeypatch):
        """Both outcomes leave a record, so a refusal is not a silent no-op."""
        from unittest.mock import Mock

        job = await self._owned(sdk)
        audit = Mock()
        monkeypatch.setattr("kiro_crew.apps.cron_sdk.sel", lambda: audit)
        svc.run_duration = 5.0
        assert await sdk.run_job_async(job.id) is True  # returns once started
        assert audit.log_api_access.call_args.kwargs["outcome"] == "ok"
        assert await sdk.run_job_async(job.id) is False  # the first still runs
        assert audit.log_api_access.call_args.kwargs["outcome"] == "refused"
        svc._claims.clear()

    @pytest.mark.asyncio
    async def test_a_failing_run_is_logged_against_the_app(self, svc, sdk, caplog):
        """Nobody awaits the dispatched run, so its failure is consumed here.

        Left unconsumed it would surface as asyncio's "Task exception was never
        retrieved" with no app or job named.
        """
        job = await self._owned(sdk)
        svc.run_raises = RuntimeError("boom")
        with caplog.at_level(logging.ERROR, logger="kiro_crew.apps.cron_sdk"):
            assert await sdk.run_job_async(job.id) is True
            for _ in range(200):  # the done callback lands on a later loop pass
                if caplog.text:
                    break
                await asyncio.sleep(0.01)
        assert "example" in caplog.text and job.id in caplog.text
        assert "boom" in caplog.text

    @pytest.mark.asyncio
    async def test_an_app_triggered_run_refreshes_the_schedule_page(self, svc, sdk):
        """A run an app starts shows up on the Schedule page like a Run click.

        The refresh lives in the shared ``CronService.trigger_run``; while the
        SDK kept its own copy of the section it had no refresh, so an
        app-triggered run started invisibly.
        """
        from unittest.mock import Mock

        job = await self._owned(sdk)
        svc._push_refresh = Mock()
        assert await sdk.run_job_async(job.id) is True
        svc._push_refresh.assert_called_once_with("crons")

    @pytest.mark.asyncio
    async def test_the_sdk_claims_through_the_shared_section(self, svc, sdk):
        """The verb defers the guard and the claim to ``CronService.trigger_run``.

        One copy of the section, shared with the manual-run route, so a fix to
        it cannot land at one trigger and miss the other.
        """
        from unittest.mock import Mock

        job = await self._owned(sdk)
        svc.trigger_run = Mock(return_value=None)  # the shared section refuses
        assert await sdk.run_job_async(job.id) is False
        svc.trigger_run.assert_called_once_with(
            job.id, expected_owner="app:example", started=ANY
        )
        assert svc.runs == []

    def test_there_is_no_sync_sibling(self):
        """Async-only on purpose: a sync form would return an un-awaited coroutine.

        Running a job is not a store write a loop-less caller can block on --
        ``CronService.run_job`` claims on the loop and spawns a loop task -- so a
        ``run_job`` added for symmetry with the other verbs would hand an app a
        coroutine that never runs.
        """
        assert not hasattr(CronSDK, "run_job")

    @pytest.mark.asyncio
    async def test_a_real_run_reaches_execution_and_keeps_the_interval(self, tmp_path):
        """Against the real service: the job fires and its interval is unchanged."""
        fired: list[str] = []

        async def on_job(job: Any) -> None:
            fired.append(job.id)

        service = CronService(base_dir=tmp_path, on_job=on_job)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        assert await sdk.run_job_async(job.id) is True

        # Wait for the run's RESULT to reach the store, not for the callback to
        # be entered. ``fired`` fills inside the callback, but ``last_status``
        # is set only after the callback returns and is persisted after that,
        # so a wait on ``fired`` raced the persist and lost on a slow disk.
        async def _saved_job() -> dict[str, Any]:
            raw = await asyncio.to_thread(service._path.read_text, encoding="utf-8")
            return json.loads(raw)["jobs"][0]

        saved = await _saved_job()
        for _ in range(500):  # the dispatched run owns its own task
            if saved.get("last_status") is not None:
                break
            await asyncio.sleep(0.01)
            saved = await _saved_job()
        assert fired == [job.id]
        assert saved["schedule"] == {
            "kind": "every",
            "every_secs": 3600,
            "at_ts": None,
            "cron_expr": None,
        }
        assert saved["id"] == job.id
        assert saved["last_status"] == "ok"

    @pytest.mark.asyncio
    async def test_a_real_foreign_job_is_refused_without_touching_the_store(
        self, tmp_path
    ):
        """Ownership is checked against the store, and a refusal writes nothing."""
        service = CronService(base_dir=tmp_path)
        service._dir.mkdir(parents=True, exist_ok=True)
        other = await CronSDK("other", service).add_job_async(
            name="other/poll", message="go", every_secs=3600
        )
        before = await asyncio.to_thread(service._path.read_bytes)
        with pytest.raises(PermissionError):
            await CronSDK("example", service).run_job_async(other.id)
        assert await asyncio.to_thread(service._path.read_bytes) == before

    @pytest.mark.asyncio
    async def test_a_stale_cache_cannot_start_another_owners_job(self, tmp_path, monkeypatch):
        """Ownership is re-checked on the job the run resolves from the store.

        ``_assert_owned`` reads the cache-only snapshot, which can trail the
        store by a poll interval. Here the cache still says this app owns the
        job while the store on disk records another owner: the run must not
        start, and its claim must be released so the job is not left occupied.
        The same job with its owner unchanged runs, so this is the owner check
        refusing and not a run that never worked. The refusal is SEL-audited as
        a ``cron_run_job`` denial, because the SDK had already audited the
        dispatch as ``ok`` off its cached check.
        """
        from unittest.mock import Mock

        audit = Mock()
        monkeypatch.setattr("kiro_crew.cron.sel.sel", lambda: audit)
        fired: list[str] = []

        async def on_job(job: Any) -> None:
            fired.append(job.id)

        service = CronService(base_dir=tmp_path, on_job=on_job)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        def _rewrite_owner(owner: str) -> None:
            data = json.loads(service._path.read_text(encoding="utf-8"))
            data["jobs"][0]["created_by"] = owner
            service._path.write_text(json.dumps(data), encoding="utf-8")

        # Another writer changes the owner on disk; the cache is not refreshed.
        await asyncio.to_thread(_rewrite_owner, "app:other")
        assert service.get_job(job.id).created_by == "app:example"

        # The call answers only after the re-check, so it reports the refusal
        # rather than the claim. Bounded: a refusal that left the answer
        # unresolved would hang here instead of failing.
        assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is False
        assert not service.is_running(job.id)  # the claim was released
        assert fired == []  # and the other owner's job never executed
        denials = [
            c.kwargs
            for c in audit.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert [(d["caller"], d["operation"], d["resources"]) for d in denials] == [
            ("app:example", "cron_run_job", job.id)
        ]

        # Control arm: with the owner back, the same path does run the job.
        # The refused run's refresh synced the cache to "app:other", so read
        # the store once more before the SDK's cache-only check.
        await asyncio.to_thread(_rewrite_owner, "app:example")
        await service.get_job_async(job.id)
        assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is True
        for _ in range(500):
            if fired:
                break
            await asyncio.sleep(0.01)
        assert fired == [job.id]

    @pytest.mark.asyncio
    async def test_a_contended_store_does_not_fall_back_to_the_cache(
        self, tmp_path, monkeypatch, caplog
    ):
        """The owner re-check fails closed when the store lock is contended.

        A plain manual-run refresh degrades to the cache when it cannot lock
        the store, but that cache is what ``_assert_owned`` already read. Here
        the store records another owner and the lock is contended: the run must
        not start, must release its claim, and must leave a denial record.
        """
        from contextlib import contextmanager
        from unittest.mock import Mock

        from kiro_crew.cron_service.store import CronStoreBusy

        fired: list[str] = []

        async def on_job(job: Any) -> None:
            fired.append(job.id)

        service = CronService(base_dir=tmp_path, on_job=on_job)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        def _rewrite_owner(owner: str) -> None:
            data = json.loads(service._path.read_text(encoding="utf-8"))
            data["jobs"][0]["created_by"] = owner
            service._path.write_text(json.dumps(data), encoding="utf-8")

        await asyncio.to_thread(_rewrite_owner, "app:other")

        @contextmanager
        def _contended():
            raise CronStoreBusy("contended")
            yield  # pragma: no cover

        monkeypatch.setattr(service, "_file_lock", _contended)
        audit = Mock()
        monkeypatch.setattr("kiro_crew.cron.sel.sel", lambda: audit)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.apps.cron_sdk"):
            # A busy store is reported as no run, so the app can try again.
            assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is False
            for _ in range(500):
                if not service.is_running(job.id) and caplog.text:
                    break
                await asyncio.sleep(0.01)
        assert not service.is_running(job.id)  # the claim was released
        assert fired == []  # the cached owner was not trusted
        denials = [
            c.kwargs
            for c in audit.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert [(d["caller"], d["resources"]) for d in denials] == [("app:example", job.id)]
        assert "store busy" in denials[0]["error"]
        assert job.id in caplog.text  # the SDK logs the failed run against the job

    @pytest.mark.asyncio
    async def test_an_unreadable_store_does_not_fall_back_to_the_cache(
        self, tmp_path, monkeypatch, caplog
    ):
        """The owner re-check fails closed when the store cannot be READ at all.

        The sibling above covers the contended lock. This is the OTHER way the
        strict refresh fails to produce store-backed state, and the quiet one:
        ``_sync`` catches the ``OSError`` from ``read_bytes``, latches
        ``_load_failed`` and RETURNS normally, deliberately keeping the cached
        jobs so an unsaved reaper mutation is not lost. The snapshot therefore
        comes back indistinguishable from a successful read of the store, and
        what it carries is the very cache ``_assert_owned`` already trusted.

        The harm that makes this more than hygiene: on disk the job has been
        reassigned to another owner, so the former owner's ``expected_owner``
        matches the stale cache and nothing else. Without the refusal the
        re-check passes on that cache and this app starts a job it does not
        own, with nothing in the log or the audit to say it happened.

        Injected at the filesystem boundary rather than by raising
        ``CronStoreUnreadable``, so the real ``_sync`` -> latch -> refusal chain
        runs. A genuinely corrupt document would NOT pin this: ``_load``'s parse
        paths empty the job list, so the run would be refused by the
        missing-job branch and the test would pass with the fix reverted.
        """
        import contextlib
        import errno
        from unittest.mock import Mock

        fired: list[str] = []

        async def on_job(job: Any) -> None:
            fired.append(job.id)

        service = CronService(base_dir=tmp_path, on_job=on_job)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        def _rewrite_owner(owner: str) -> None:
            data = json.loads(service._path.read_text(encoding="utf-8"))
            data["jobs"][0]["created_by"] = owner
            service._path.write_text(json.dumps(data), encoding="utf-8")

        # Another writer reassigns the job, and the cache is not told.
        await asyncio.to_thread(_rewrite_owner, "app:other")
        assert [j.created_by for j in service.list_jobs(True)] == [
            "app:example"
        ], "precondition: the cache must still name THIS app as the owner"

        @contextlib.contextmanager
        def _read_failures_on(target: Path):
            """``read_bytes()`` raises EIO for *target* only; writes keep working."""
            real = Path.read_bytes

            def failing(self: Path) -> bytes:
                if self == target:
                    raise OSError(errno.EIO, "Input/output error")
                return real(self)

            Path.read_bytes = failing  # type: ignore[method-assign]
            try:
                yield
            finally:
                Path.read_bytes = real  # type: ignore[method-assign]

        audit = Mock()
        monkeypatch.setattr("kiro_crew.cron.sel.sel", lambda: audit)

        with _read_failures_on(service._path):
            with caplog.at_level(logging.ERROR, logger="kiro_crew.apps.cron_sdk"):
                # An unreadable store is reported as no run, like a busy one.
                assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is False
                for _ in range(500):
                    if not service.is_running(job.id) and caplog.text:
                        break
                    await asyncio.sleep(0.01)
            assert service._load_failed, "the read failure itself must set the latch"

        assert not service.is_running(job.id)  # the claim was released
        assert fired == []  # the cached owner was not trusted
        denials = [
            c.kwargs
            for c in audit.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert [(d["caller"], d["resources"]) for d in denials] == [("app:example", job.id)]
        assert "store unreadable" in denials[0]["error"]
        assert job.id in caplog.text  # the SDK logs the failed run against the job

    @pytest.mark.asyncio
    async def test_a_missing_store_does_not_fall_back_to_the_cache(self, tmp_path):
        """The owner re-check fails closed when the store file is GONE.

        The third way a strict refresh fails to produce store-backed state, and
        the only one neither sibling above covers. A missing store is not an
        unreadable one, so ``_sync`` takes its missing-file branch: it CLEARS
        the ``_load_failed`` latch and returns, keeping the cached jobs so an
        unsaved reaper mutation survives. ``raise_if_store_unreadable`` then has
        nothing to fire on and the cache reaches the re-check looking exactly
        like a store-backed read.

        The harm is worse than the unreadable case. The run does not merely
        start on a cache nothing confirmed: ``_merge_job_result`` saves when it
        ends, so the cached job is written back and a job an external writer
        deleted is resurrected.
        """
        fired: list[str] = []

        async def on_job(job: Any) -> None:
            fired.append(job.id)

        service = CronService(base_dir=tmp_path, on_job=on_job)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        # An external writer removes the store, and the cache is not told.
        await asyncio.to_thread(service._path.unlink)
        assert [j.id for j in service.list_jobs(True)] == [
            job.id
        ], "precondition: the cache must still hold the job the store no longer has"

        # Nothing in the store backs this app's ownership, so no run starts.
        assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is False
        for _ in range(500):
            if not service.is_running(job.id):
                break
            await asyncio.sleep(0.01)

        assert not service.is_running(job.id)  # the claim was released
        assert fired == []  # the cached job was not trusted
        assert not service._path.exists()  # and the deleted job was not resurrected

    @pytest.mark.asyncio
    async def test_a_run_parked_in_its_store_refresh_is_tracked(self, tmp_path):
        """The dispatched wrapper is attached to the claim before it can park.

        ``run_job``'s coroutine spends its first phase in an offloaded store
        refresh, and only after that does it set the claim's task to the inner
        run. ``attach_run_task`` is what occupies that window: without it the
        claim carries no task at all while the refresh is in flight, so
        ``stop()`` (which cancels and awaits exactly the claims' tasks) and
        ``discard_finished_run`` (which asks whether the claim's task is done)
        both have nothing to act on for a run that is genuinely in progress.

        This is the mutation pin for that one line: deleting
        ``attach_run_task`` passes every other test in this class.
        """
        service = CronService(base_dir=tmp_path)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        import threading

        refreshing = threading.Event()  # the worker thread reached the refresh
        release = threading.Event()  # the test lets the refresh finish
        real_snapshot = service._synced_snapshot

        def parked_snapshot(*args: Any, **kwargs: Any) -> Any:
            refreshing.set()
            release.wait(10)
            return real_snapshot(*args, **kwargs)

        service._synced_snapshot = parked_snapshot  # type: ignore[method-assign]
        # The call waits for the refresh it is parked in, so it runs as a task.
        call = asyncio.create_task(sdk.run_job_async(job.id))
        for _ in range(500):  # the refresh runs on a worker thread
            if refreshing.is_set():
                break
            await asyncio.sleep(0.01)
        assert refreshing.is_set()

        tracked = service._claims[job.id].task
        assert tracked is not None  # reds when attach_run_task is deleted
        assert not tracked.done()
        # The healthy side: once the refresh completes the run finishes and the
        # claim is released, so the pin is not passing on a permanently stuck run.
        assert not call.done()  # no answer before the refresh has finished
        release.set()
        assert await asyncio.wait_for(call, 10) is True
        await asyncio.wait_for(tracked, 10)
        for _ in range(500):
            if job.id not in service._claims:
                break
            await asyncio.sleep(0.01)
        assert job.id not in service._claims

    @pytest.mark.asyncio
    async def test_a_run_cancelled_before_its_first_step_is_not_reported_started(
        self, tmp_path, monkeypatch
    ):
        """A run taken before its first step answers False, not True.

        ``stop()`` at shutdown or ``cancel()`` can cancel the spawned run after
        ``create_task`` but before its first timeslice. Such a run executes
        nothing and writes no history, so a ``True`` resolved at spawn would
        tell the app a run started that never did. ``started`` is resolved by
        the run's own first step instead, and the wrapper's done callback
        answers False when that step never comes.
        """
        fired: list[str] = []

        async def on_job(job: Any) -> None:
            fired.append(job.id)

        service = CronService(base_dir=tmp_path, on_job=on_job)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        real_create_task = asyncio.create_task

        def create_then_cancel(coro: Any, *args: Any, **kwargs: Any) -> Any:
            # Stand-in for stop() landing in the gap: the inner run's task is
            # cancelled before the loop gives it its first step.
            task = real_create_task(coro, *args, **kwargs)
            if getattr(coro, "__qualname__", "").endswith("_run_job_isolated"):
                task.cancel()
            return task

        # A scoped context, not ``monkeypatch.undo()``: the autouse fixtures
        # patch their MCP-approval and session-lock globals onto this same
        # shared instance, so undoing it would restore those too and run the
        # control call below with its fixture isolation already removed. The
        # context owns only this one replacement and reverts only that.
        with monkeypatch.context() as patched:
            patched.setattr(asyncio, "create_task", create_then_cancel)
            assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is False

        assert fired == []
        for _ in range(500):
            if job.id not in service._claims:
                break
            await asyncio.sleep(0.01)
        assert job.id not in service._claims
        raw = await asyncio.to_thread(service._path.read_text, encoding="utf-8")
        assert json.loads(raw)["jobs"][0].get("last_status") is None
        # The healthy side: the same job, uncancelled, starts and reports True.
        assert await asyncio.wait_for(sdk.run_job_async(job.id), 10) is True

    @pytest.mark.asyncio
    async def test_a_caller_cancelled_while_waiting_still_leaves_an_audit(
        self, tmp_path, monkeypatch
    ):
        """The dispatch is audited before the wait, so cancelling the caller
        cannot leave a detached run with no ``cron_run_job`` record.
        """
        import threading
        from unittest.mock import Mock

        service = CronService(base_dir=tmp_path)
        service._dir.mkdir(parents=True, exist_ok=True)
        sdk = CronSDK("example", service)
        job = await sdk.add_job_async(name="example/poll", message="go", every_secs=3600)

        audit = Mock()
        monkeypatch.setattr("kiro_crew.apps.cron_sdk.sel", lambda: audit)
        refreshing = threading.Event()
        release = threading.Event()
        real_snapshot = service._synced_snapshot

        def parked_snapshot(*args: Any, **kwargs: Any) -> Any:
            refreshing.set()
            release.wait(10)
            return real_snapshot(*args, **kwargs)

        service._synced_snapshot = parked_snapshot  # type: ignore[method-assign]
        call = asyncio.create_task(sdk.run_job_async(job.id))
        for _ in range(500):
            if refreshing.is_set():
                break
            await asyncio.sleep(0.01)
        assert refreshing.is_set()
        tracked = service._claims[job.id].task

        call.cancel()  # a timeout around the caller, mid-wait
        with pytest.raises(asyncio.CancelledError):
            await call
        outcomes = [
            c.kwargs["outcome"]
            for c in audit.log_api_access.call_args_list
            if c.kwargs.get("operation") == "cron_run_job"
        ]
        assert outcomes == ["ok"]  # reds when the audit follows the wait
        # The run is detached from the caller and goes on regardless.
        release.set()
        await asyncio.wait_for(tracked, 10)

    @pytest.mark.asyncio
    async def test_a_disabled_apps_job_does_not_run_however_it_is_triggered(
        self, tmp_path
    ):
        """Measures the claim the docstring and the specs make about the gate.

        Both say a manual run takes the same cron callback, so
        ``vet_job_at_fire_time`` still decides execution and a job owned by a
        DISABLED app does not run however it was triggered. That is a
        consequence an app author can act on, so it is measured through the real
        gateway callback and the real gate rather than restated.

        Both directions are asserted: disabled refuses and records the refusal,
        enabled executes. A one-directional test would also pass on a gate that
        refused everything.
        """
        from test_cron_gateway_integration import _make_gw

        app = "example"

        async def _callback_from_gateway() -> Any:
            """The gateway's own cron callback, captured as it builds it."""
            gw = _make_gw()
            captured: dict[str, Any] = {}

            def capture_cron(on_job: Any = None, **kwargs: Any) -> Any:
                captured["cb"] = on_job
                svc = MagicMock()
                svc.start = AsyncMock()
                svc.remove_job_async = AsyncMock(return_value=True)
                return svc

            with patch("kiro_crew.slack.gateway.CronService") as mock_cron_cls:
                mock_cron_cls.create = AsyncMock(side_effect=capture_cron)
                await gw._init_cron()
            assert captured.get("cb") is not None
            return captured["cb"]

        async def _run_with_app_enabled(enabled: bool) -> tuple[Any, Any]:
            service = CronService(base_dir=tmp_path / str(enabled), on_job=await _callback_from_gateway())
            service._dir.mkdir(parents=True, exist_ok=True)
            sdk = CronSDK(app, service)
            job = await sdk.add_job_async(
                name=f"{app}/dispatch", message="", command="echo hello", every_secs=3600
            )
            # ``run_job_async`` answers once the run has STARTED, so neither it
            # nor ``mock_run.called`` means the callback has finished recording.
            # The run's own task does, so capture it where the dispatch attaches
            # it and await that instead of sleeping a fixed delay: waiting a
            # guessed 50ms let the patch context close over a still-live
            # detached run, which then raced teardown.
            dispatched: list[Any] = []
            real_attach = service.attach_run_task

            def capture_attach(job_id: str, task: Any) -> None:
                dispatched.append(task)
                real_attach(job_id, task)

            service.attach_run_task = capture_attach  # type: ignore[method-assign]
            with (
                patch(
                    "kiro_crew.slack.gateway.run_command_sandboxed",
                    return_value={"status": "ok", "output": "hello\n", "exit_code": 0},
                ) as mock_run,
                patch("kiro_crew.slack.gateway.sel"),
                # Neutralize the sibling fire-time gates so only the app gate decides.
                patch(
                    "kiro_crew.mcp_cron._vet_cron_capability_governance", return_value=None
                ),
                patch("kiro_crew.mcp_cron._vet_command_governance", return_value=None),
                patch("kiro_crew.apps.manager.app_enabled_state", return_value=enabled),
            ):
                assert await sdk.run_job_async(job.id) is True
                assert dispatched  # reds when the dispatch stops owning a task
                await asyncio.wait_for(dispatched[0], 10)
            return job, mock_run

        job, mock_run = await _run_with_app_enabled(False)
        mock_run.assert_not_called()  # the manual run did not execute
        assert job.last_status == "error"
        assert app in (job.last_error or "")
        assert "disabled" in (job.last_error or "")

        job, mock_run = await _run_with_app_enabled(True)
        mock_run.assert_called_once()  # the falsified direction: the gate still passes work
