"""Cron SDK — app-scoped cron job management.

Wraps CronService with ownership enforcement so apps can only manage
their own cron jobs. Jobs are tagged with ``created_by = "app:{app_name}"``
for filtering and permission checks.

Concurrency safety:
- CronService uses atomic_write (write-to-tmp + os.replace) for persistence
- Within a single event loop, Python's GIL serializes in-memory mutations
- Cross-process safety is handled by CronService's existing fcntl.flock
- If a cron job is executing when remove_all() is called (e.g. on disable),
  the running job completes its current iteration but won't be scheduled again
"""
from __future__ import annotations

import logging
from typing import Any

from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


class CronSDK:
    """App-scoped cron job management."""

    def __init__(self, app_name: str, cron_service: Any) -> None:
        self._app_name = app_name
        self._cron = cron_service
        self._owner_prefix = f"app:{app_name}"

    @property
    def app_name(self) -> str:
        return self._app_name

    def add_job(
        self,
        name: str,
        message: str,
        *,
        every_secs: int | None = None,
        cron_expr: str | None = None,
        agent: str = "",
        agent_sequence: list[str] | None = None,
        env: dict[str, str] | None = None,
        persistent_session: bool = True,
        silent: bool = False,
    ) -> Any:
        """Create a cron job owned by this app.

        Returns the created CronJob object.
        """
        # Build kwargs for add_job — only pass what's supported
        kwargs: dict[str, Any] = {
            "name": name,
            "message": message,
        }
        if every_secs is not None:
            kwargs["every_secs"] = every_secs
        if cron_expr is not None:
            kwargs["cron_expr"] = cron_expr

        job = self._cron.add_job(**kwargs)

        # Tag ownership
        job.created_by = self._owner_prefix

        # Set extended fields
        if agent:
            job.agent_id = agent
        if agent_sequence:
            job.agent_sequence = agent_sequence
        if env:
            job.env = env
        job.persistent_session = persistent_session
        job.silent = silent

        self._cron._save()
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_add_job",
            outcome="ok",
            resources=job.id,
        )
        logger.info("App %s created cron job: %s (id=%s)", self._app_name, name, job.id)
        return job

    def list_jobs(self) -> list[Any]:
        """List only jobs owned by this app."""
        return [
            j for j in self._cron.list_jobs(include_disabled=True)
            if getattr(j, "created_by", "") == self._owner_prefix
        ]

    def remove_job(self, job_id: str) -> bool:
        """Remove a job only if owned by this app.

        Raises PermissionError if the job belongs to a different app.
        """
        job = self._find_owned_job(job_id)
        if not job:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation="cron_remove_job",
                outcome="denied",
                resources=job_id,
                error="ownership violation",
            )
            raise PermissionError(
                f"Job {job_id} not owned by app {self._app_name}"
            )
        result = self._cron.remove_job(job_id)
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_remove_job",
            outcome="ok",
            resources=job_id,
        )
        logger.info("App %s removed cron job: %s", self._app_name, job_id)
        return result

    def update_job(self, job_id: str, **kwargs: Any) -> Any:
        """Update a job only if owned by this app.

        Raises PermissionError if the job belongs to a different app.
        Returns the updated CronJob or None.
        """
        job = self._find_owned_job(job_id)
        if not job:
            sel().log_api_access(
                caller=f"app:{self._app_name}",
                operation="cron_update_job",
                outcome="denied",
                resources=job_id,
                error="ownership violation",
            )
            raise PermissionError(
                f"Job {job_id} not owned by app {self._app_name}"
            )
        result = self._cron.update_job(job_id, **kwargs)
        sel().log_api_access(
            caller=f"app:{self._app_name}",
            operation="cron_update_job",
            outcome="ok",
            resources=job_id,
        )
        logger.info("App %s updated cron job: %s", self._app_name, job_id)
        return result

    def remove_all(self) -> int:
        """Remove all jobs owned by this app.

        Called on disable/uninstall. Returns count of removed jobs.
        Uses self.remove_job() to ensure SEL audit for each removal.
        """
        jobs = self.list_jobs()
        count = 0
        for job in jobs:
            self.remove_job(job.id)
            count += 1
        if count:
            logger.info("App %s removed %d cron job(s)", self._app_name, count)
        return count

    def _find_owned_job(self, job_id: str) -> Any | None:
        """Find a job by ID, only if owned by this app."""
        for job in self._cron.list_jobs(include_disabled=True):
            if job.id == job_id and getattr(job, "created_by", "") == self._owner_prefix:
                return job
        return None
