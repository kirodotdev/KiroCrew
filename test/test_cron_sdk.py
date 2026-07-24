"""Property tests for CronSDK ownership enforcement.

Feature: app-sdk-gateway-hooks
Properties 3, 4, 5, 6: Cron job creation, ownership, filtering, cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.cron_sdk import CronSDK

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


class MockCronService:
    def __init__(self) -> None:
        self._jobs: list[MockCronJob] = []
        self._next_id = 1

    def add_job(self, **kwargs: Any) -> MockCronJob:
        # Mirror CronService.add_job register-paused semantics: enabled=False
        # creates the job already paused (user_paused=True) at creation.
        job = MockCronJob(
            id=f"job-{self._next_id}",
            user_paused=not kwargs.get("enabled", True),
            **kwargs,
        )
        self._next_id += 1
        self._jobs.append(job)
        return job

    def list_jobs(self, include_disabled: bool = False) -> list[MockCronJob]:
        if include_disabled:
            return list(self._jobs)
        return [j for j in self._jobs if j.enabled]

    def remove_job(self, job_id: str) -> bool:
        for i, j in enumerate(self._jobs):
            if j.id == job_id:
                self._jobs.pop(i)
                return True
        return False

    def update_job(self, job_id: str, **kwargs: Any) -> MockCronJob | None:
        for j in self._jobs:
            if j.id == job_id:
                for k, v in kwargs.items():
                    setattr(j, k, v)
                return j
        return None

    def _save(self) -> None:
        pass


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

        job = sdk.add_job(
            name=job_name,
            message="test",
            cron_expr="* * * * *",
            agent_sequence=agent_seq,
            env=env,
            persistent_session=persistent,
            silent=silent,
        )

        assert job.created_by == f"app:{app_name}"
        assert job.agent_sequence == agent_seq
        assert job.env == env
        assert job.persistent_session == persistent
        assert job.silent == silent

    def test_disabled_job_registers_paused(self) -> None:
        """enabled=False creates the job in a paused, user-resumable state."""
        svc = MockCronService()
        sdk = CronSDK("my-app", svc)

        job = sdk.add_job(
            name="my-app/nightly-run",
            message="",
            cron_expr="0 22 * * *",
            enabled=False,
        )

        assert job.enabled is False
        assert job.user_paused is True

    def test_enabled_default_registers_active(self) -> None:
        """Default add_job (no enabled kwarg) creates an active job."""
        svc = MockCronService()
        sdk = CronSDK("my-app", svc)

        job = sdk.add_job(name="my-app/refresh", message="go", cron_expr="* * * * *")

        assert job.enabled is True
        assert getattr(job, "user_paused", False) is False

    def test_paused_at_registration_job_can_be_resumed(self) -> None:
        """A job registered disabled can be re-enabled (resumed) via update_job."""
        svc = MockCronService()
        sdk = CronSDK("my-app", svc)

        job = sdk.add_job(
            name="my-app/nightly-run",
            message="",
            cron_expr="0 22 * * *",
            enabled=False,
        )
        assert job.enabled is False

        updated = sdk.update_job(job.id, enabled=True, user_paused=False)

        assert updated is not None
        assert updated.enabled is True
        assert updated.user_paused is False
        # Resumed job shows up in the active (non-disabled) list again.
        assert job in svc.list_jobs()


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

        job = sdk_a.add_job(name="test-job", message="msg", cron_expr="* * * * *")

        with pytest.raises(PermissionError):
            sdk_b.remove_job(job.id)

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

        job = sdk_a.add_job(name="test-job", message="msg", cron_expr="* * * * *")

        with pytest.raises(PermissionError):
            sdk_b.update_job(job.id, message="hacked")

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
            sdk_a.add_job(name=f"a-job-{i}", message="a", cron_expr="* * * * *")
        for i in range(n_b):
            sdk_b.add_job(name=f"b-job-{i}", message="b", cron_expr="* * * * *")

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
            sdk.add_job(name=f"job-{i}", message="msg", cron_expr="* * * * *")

        assert len(sdk.list_jobs()) == n_jobs
        removed = sdk.remove_all()
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

        sdk_a.add_job(name="a-job", message="a", cron_expr="* * * * *")
        sdk_b.add_job(name="b-job", message="b", cron_expr="* * * * *")

        sdk_a.remove_all()
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
            sdk.add_job(
                name="exfil",
                message="",
                command="cat ~/.aws/credentials",
                cron_expr="* * * * *",
            )

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
            sdk.add_job(
                name="exfil",
                message="",
                script="~/.kirocrew/crons/evil.py:run",
                cron_expr="* * * * *",
            )

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
            sdk.add_job(
                name="escape",
                message="",
                script="/etc/passwd:run",
                cron_expr="* * * * *",
            )

        # Deny-by-default: nothing was added to the service.
        assert svc._jobs == []
