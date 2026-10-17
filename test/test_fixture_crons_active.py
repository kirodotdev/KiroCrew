"""Consumer assertions for the ``crons-active`` seeded-home fixture."""

import json
import re
from pathlib import Path

from kiro_crew.cron import CronService
from kiro_crew.testing.fixtures import seeded_home

_WEBSITE_SRC = Path(__file__).resolve().parents[1] / "website" / "src"
_PRESETS_TSX = _WEBSITE_SRC / "utils" / "schedulePresets.tsx"
_EN_CATALOG = _WEBSITE_SRC / "i18n" / "locales" / "en.manual.json"


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


def _shipped_preset_prompts() -> dict[str, str]:
    """Map each ``SCHEDULE_PRESETS`` id to its English prompt.

    ``presetCanonicalPrompt`` resolves ``utils.schedulePresets.<id>_message`` in
    the English catalog, with the id's dashes spelled as underscores.
    """
    source = _PRESETS_TSX.read_text(encoding="utf-8")
    start = source.index("export const SCHEDULE_PRESETS")
    block = source[start : source.index("\n]\n", start)]
    ids = re.findall(r"^    id: '([a-z0-9-]+)',$", block, flags=re.MULTILINE)
    catalog = json.loads(_EN_CATALOG.read_text(encoding="utf-8"))["utils"]["schedulePresets"]
    return {i: catalog.get(f"{i.replace('-', '_')}_message", "") for i in ids}


def test_crons_active_seeds_a_job_whose_source_template_has_moved() -> None:
    """``fixture-cron-hourly`` carries what makes ``templateUpdate`` return a result.

    The Schedule page shows ``TemplateUpdatedNotice`` only when a job's
    ``source_preset`` names a shipped preset and its ``source_template_prompt``
    snapshot differs from that preset's current English prompt
    (``templateUpdate`` in ``website/src/utils/schedulePresets.tsx``).
    """
    prompts = _shipped_preset_prompts()
    assert prompts, "schedulePresets.tsx lists no preset ids"

    with seeded_home("crons-active") as home:
        service = CronService(base_dir=home)
        jobs = {job.id: job for job in service.list_jobs(include_disabled=True)}

    job = jobs["fixture-cron-hourly"]
    assert job.source_preset in prompts, f"{job.source_preset!r} is not a shipped preset id"
    assert prompts[job.source_preset], "the preset has no English prompt to compare against"
    assert job.source_template_prompt, "the job carries no template snapshot"
    assert job.source_template_prompt != prompts[job.source_preset]
