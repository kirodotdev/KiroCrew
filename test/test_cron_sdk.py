"""Property tests for CronSDK ownership enforcement.

Feature: app-sdk-gateway-hooks
Properties 3, 4, 5, 6: Cron job creation, ownership, filtering, cleanup.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
