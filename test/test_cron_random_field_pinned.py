"""A croniter random (``R``) field is one fixed pick per job.

croniter draws a fresh value for ``R`` every time it reads an expression, so a
stored ``R 9 * * *`` fired at a different minute on each scheduler check (some
days never, some days twice) and its preview changed on every read. The field
is pinned to croniter's ``H`` hash of the job id: the expression is still
accepted, the same job always gets the same pick, and a job already stored
with ``R`` reads the same pick on every load.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew import change_card_catalog as catalog
from kiro_crew.cron import CronService
from kiro_crew.cron_service import schedule
from kiro_crew.cron_service.fields import build_job
from kiro_crew.cron_service.model import CronSchedule
from kiro_crew.cron_service.schedule import compute_next_run_ts, validate_cron_expr
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.testing.clock import ManualClock

#: A fixed instant, so no clock is read.
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
#: What an hour-9 random minute is stored as: one concrete minute.
_ONE_MINUTE_AT_9 = r"^\d+ 9 \* \* \*$"


@pytest.mark.parametrize(
    "expr,shape",
    [
        ("R 9 * * *", r"^\d+ 9 \* \* \*$"),
        ("R(0-29) 9 * * *", r"^\d+ 9 \* \* \*$"),
        ("R/15 * * * *", r"^\d+(,\d+){3} \* \* \* \*$"),
        ("0 R * * R", r"^0 \d+ \* \* \d$"),
        ("r 9 * * mon-fri", r"^\d+ 9 \* \* mon-fri$"),
    ],
)
def test_a_random_field_is_one_stable_valid_pick(expr, shape):
    pinned = schedule.pin_random_cron_fields(expr, "abc12345")

    assert pinned == schedule.pin_random_cron_fields(expr, "abc12345")
    assert validate_cron_expr(pinned)
    assert re.match(shape, pinned)


@pytest.mark.parametrize(
    "expr", ["0 9 * mar *", "0 9 * * fri", "*/5 * * * *", "@daily", "0 9 * * thu"]
)
def test_an_expression_without_a_random_field_is_unchanged(expr):
    assert schedule.pin_random_cron_fields(expr, "abc12345") == expr


def test_two_jobs_with_the_same_random_expression_can_differ():
    picks = {schedule.pin_random_cron_fields("R 9 * * *", f"job{n:05d}") for n in range(20)}

    assert len(picks) > 1


def test_a_created_job_stores_its_pick_and_previews_the_same_run():
    job = build_job("r", "m", cron_expr="R 9 * * *")

    assert re.match(_ONE_MINUTE_AT_9, job.schedule.cron_expr)
    previews = {compute_next_run_ts(job, now=_NOW) for _ in range(5)}
    assert len(previews) == 1


def test_an_update_to_a_random_expression_is_pinned(tmp_path):
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="r", message="m", every_secs=3600)

    svc.update_job(job.id, cron_expr="R 9 * * *")

    stored = CronService(base_dir=tmp_path).get_job(job.id)
    assert re.match(_ONE_MINUTE_AT_9, stored.schedule.cron_expr)


def test_a_stored_random_expression_loads_as_the_same_pick(tmp_path):
    svc = CronService(base_dir=tmp_path)
    path = Path(svc._path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "abc12345",
        "name": "legacy",
        "message": "m",
        "schedule": {"kind": "cron", "cron_expr": "R 9 * * *"},
    }
    path.write_text(json.dumps({"version": 2, "jobs": [record]}), encoding="utf-8")

    loads = {
        CronService(base_dir=tmp_path).get_job("abc12345").schedule.cron_expr for _ in range(3)
    }

    assert len(loads) == 1
    assert re.match(_ONE_MINUTE_AT_9, loads.pop())


def test_an_update_card_previews_the_minute_the_job_will_run(monkeypatch):
    # The preview and the expected value both read the schedule module's clock;
    # one frozen instant keeps them on the same day's occurrence.
    ManualClock(start=_NOW).install(monkeypatch, schedule, datetime=True)
    params = {"id": "abc12345", "fields": {"cron_expr": "R 9 * * *", "timezone": "UTC"}}

    def preview() -> float:
        context = asyncio.run(
            cards.read_context(
                catalog.KIND_SCHEDULE_UPDATE, params, {"fields": {}}, state=None, app=None
            )
        )
        return context["next_run_at"]

    stored = schedule.pin_random_cron_fields("R 9 * * *", "abc12345")
    assert {preview() for _ in range(5)} == {cards.next_run_at(stored, "UTC")}


def test_a_create_card_shows_no_next_run_for_a_random_field():
    params = {"name": "r", "message": "m", "cron_expr": "R 9 * * *", "timezone": "UTC"}
    context = asyncio.run(
        cards.read_context(catalog.KIND_SCHEDULE_CREATE, params, {}, state=None, app=None)
    )

    assert "next_run_at" not in context
    assert context["timezone"] == "UTC"


def test_an_update_card_reproposing_the_stored_random_schedule_changes_nothing():
    from kiro_crew.change_card_catalog_plans import build_preview

    stored = schedule.pin_random_cron_fields("R 9 * * *", "abc12345")
    snapshot = {"exists": True, "name": "r", "fields": {"cron_expr": stored, "message": "m"}}
    same = {"id": "abc12345", "fields": {"cron_expr": "R 9 * * *"}}
    with pytest.raises(catalog.CardCatalogError) as caught:
        build_preview(catalog.KIND_SCHEDULE_UPDATE, same, snapshot, {})
    assert caught.value.code == "no_change"

    edit = {"id": "abc12345", "fields": {"cron_expr": "R 9 * * *", "message": "n"}}
    out = build_preview(catalog.KIND_SCHEDULE_UPDATE, edit, snapshot, {})
    assert [c["field"] for c in out["changes"]] == ["message"]


def test_an_import_of_the_same_random_schedule_matches_the_stored_job():
    from kiro_crew.onboarding_import import _same_schedule

    job = build_job("digest", "m", cron_expr="R 9 * * *")
    payload = {"name": "digest", "message": "m", "timezone": job.timezone, "cron_expr": "R 9 * * *"}

    assert _same_schedule(job, payload)


def test_an_app_resync_of_the_same_random_schedule_overwrites_nothing():
    from kiro_crew.apps.bridges_crons import _overwritten_fields

    job = build_job("digest", "m", cron_expr="R 9 * * *")

    assert _overwritten_fields(job, {"cron_expr": "R 9 * * *"}) == []


@pytest.mark.parametrize("expr", ["0 0 R 2 *", "0 0 R * *", "0 0 r * *"])
def test_a_random_day_of_the_month_is_a_day_every_month_has(expr):
    for n in range(300):
        pinned = schedule.pin_random_cron_fields(expr, f"job{n:05d}")
        day = int(pinned.split()[2])
        assert 1 <= day <= 28, pinned
        assert schedule.never_fires_warning(CronSchedule(kind="cron", cron_expr=pinned)) == ""
