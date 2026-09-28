"""Consumer assertions for the ``crons-active`` seeded-home fixture."""

from kiro_crew.cron import CronService
from kiro_crew.testing.fixtures import seeded_home


def test_crons_active_loads_and_filters_multiple_active_jobs() -> None:
    """The production cron loader preserves order and excludes the paused row."""
    with seeded_home("crons-active") as home:
        service = CronService(base_dir=home)

        all_jobs = service.list_jobs(include_disabled=True)
        enabled_jobs = service.list_jobs()

        assert [job.id for job in all_jobs] == [
            "fixture-cron-morning",
            "fixture-cron-hourly",
            "fixture-cron-paused",
        ]
        assert [job.id for job in enabled_jobs] == [
            "fixture-cron-morning",
            "fixture-cron-hourly",
        ]
        assert len(all_jobs) == 3
        assert len(enabled_jobs) == 2
        assert all(job.enabled for job in enabled_jobs)
        assert all_jobs[2].enabled is False
        assert all_jobs[2].user_paused is True
        assert [job.schedule.cron_expr for job in all_jobs] == [
            "0 9 * * 1-5",
            "0 * * * *",
            "0 17 * * 5",
        ]
        assert all(job.schedule.kind == "cron" for job in all_jobs)
        assert all(job.script == "" for job in all_jobs)
