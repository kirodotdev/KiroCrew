"""Tests for one-shot job creation over `POST /api/crons`.

The handler's validation mirrors `CRON_ADD_SCHEMA` "so the REST + tool paths
validate identically", so a body the `cron_add` MCP tool accepts must be accepted
here too and must land on the same instant. These tests pin that parity for the
one-shot fields: same names, same precedence (`at`, then `delay`, then
`at_time`), the same derived `delete_after_run`, and the refusals the schema
alone does not catch.

Mirrors test_dashboard_cron_folder_id.py's create-path structure.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService, compute_next_run_ts
from kiro_crew.dashboard.handlers import api_crons_create
from kiro_crew.validation import CRON_ADD_SCHEMA, MAX_SHORT_STRING


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _body(resp) -> dict:
    return json.loads(resp.body)


class TestOneShotAccepted:
    """A one-shot fire time is a valid schedule on its own."""

    @pytest.mark.asyncio
    async def test_absolute_at_creates_self_deleting_job(self):
        crons = CronService()
        when = time.time() + 3600
        resp = await api_crons_create(
            _create_request({"name": "later", "message": "ping", "at": when}, crons)
        )
        assert resp.status == 200
        job = crons.list_jobs()[0]
        assert job.schedule.at_ts == pytest.approx(when)
        # Derived, never accepted from the body: a one-shot that outlives its
        # single fire time can never run again.
        assert job.delete_after_run is True
        assert job.schedule.every_secs is None
        assert job.schedule.cron_expr is None
        # Stored is not the same as schedulable — assert the scheduler resolves a
        # next run at the requested instant, so the job actually fires rather
        # than sitting in the store with no due time.
        assert compute_next_run_ts(job) == pytest.approx(when)

    @pytest.mark.asyncio
    async def test_delay_is_relative_to_now(self):
        crons = CronService()
        before = time.time()
        resp = await api_crons_create(
            _create_request({"name": "soon", "message": "ping", "delay": 120}, crons)
        )
        assert resp.status == 200
        at_ts = crons.list_jobs()[0].schedule.at_ts
        assert before + 120 <= at_ts <= time.time() + 120

    @pytest.mark.asyncio
    async def test_at_time_human_string_is_parsed(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "human", "message": "ping", "at_time": "in 45 minutes"}, crons)
        )
        assert resp.status == 200
        at_ts = crons.list_jobs()[0].schedule.at_ts
        assert at_ts == pytest.approx(time.time() + 45 * 60, abs=30)

    @pytest.mark.asyncio
    async def test_at_wins_over_delay_and_at_time(self):
        """Precedence matches cron_add: `at`, then `delay`, then `at_time`.

        Pinned because a body carrying more than one must not mean different
        instants depending on which entry point received it.
        """
        crons = CronService()
        when = time.time() + 7200
        resp = await api_crons_create(
            _create_request(
                {
                    "name": "precedence",
                    "message": "ping",
                    "at": when,
                    "delay": 60,
                    "at_time": "in 5 minutes",
                },
                crons,
            )
        )
        assert resp.status == 200
        assert crons.list_jobs()[0].schedule.at_ts == pytest.approx(when)

    @pytest.mark.asyncio
    async def test_delay_wins_over_at_time(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request(
                {"name": "mid", "message": "ping", "delay": 300, "at_time": "in 5 hours"},
                crons,
            )
        )
        assert resp.status == 200
        at_ts = crons.list_jobs()[0].schedule.at_ts
        assert at_ts == pytest.approx(time.time() + 300, abs=30)


class TestRecurringUnaffected:
    """The recurring paths keep precedence over any one-shot field."""

    @pytest.mark.asyncio
    async def test_every_still_creates_a_repeating_job(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "loop", "message": "ping", "every": 3600}, crons)
        )
        assert resp.status == 200
        job = crons.list_jobs()[0]
        assert job.schedule.every_secs == 3600
        assert job.delete_after_run is False

    @pytest.mark.asyncio
    async def test_every_takes_precedence_over_at(self):
        """`every` is checked first, so a body with both stays recurring.

        Asserted rather than assumed: silently converting a recurring job to a
        one-shot would delete it after its first run.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request(
                {"name": "both", "message": "ping", "every": 3600, "at": time.time() + 60},
                crons,
            )
        )
        assert resp.status == 200
        job = crons.list_jobs()[0]
        assert job.schedule.every_secs == 3600
        assert job.delete_after_run is False


class TestOneShotRefusals:
    """Every refusal carries a machine-readable `code` (error-code contract)."""

    @pytest.mark.asyncio
    async def test_no_schedule_at_all_names_the_one_shot_fields(self):
        crons = CronService()
        resp = await api_crons_create(_create_request({"name": "naked", "message": "ping"}, crons))
        assert resp.status == 400
        body = _body(resp)
        assert body["code"] == "missing_schedule"
        # The message must advertise the new fields, or a caller reading only the
        # error keeps guessing at `every`.
        assert "at_time" in body["error"]
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_past_at_is_refused(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "gone", "message": "ping", "at": time.time() - 60}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "at_in_past"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_delay_below_schema_floor_is_refused(self):
        """`delay` carries `min_val=1` in the schema, so 0 and negatives are out.

        The refusal names `delay` rather than reporting a past instant: the
        problem is the field, and `at_in_past` would point at the wrong input.
        """
        crons = CronService()
        for bad in (0, -600):
            resp = await api_crons_create(
                _create_request({"name": "back", "message": "ping", "delay": bad}, crons)
            )
            assert resp.status == 400
            assert _body(resp)["code"] == "invalid_delay"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_millisecond_at_is_refused(self):
        """`Date.now()` without the /1000 is the ordinary caller mistake.

        1.75e12 is finite and far-future, so only the schema's ceiling catches
        it. Left through, `format_schedule` renders it with
        `datetime.fromtimestamp` inside the comprehension that serializes EVERY
        job, so one such record turns the whole listing into a 500.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "millis", "message": "ping", "at": 1.75e12}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_at_above_schema_ceiling_is_refused(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "far", "message": "ping", "at": 1e20}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at"

    @pytest.mark.asyncio
    async def test_delay_above_schema_ceiling_is_refused(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "distant", "message": "ping", "delay": 86400 * 40}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_delay"

    @pytest.mark.asyncio
    async def test_unbounded_relative_at_time_is_refused(self):
        """`at_time`'s relative form has no bound of its own.

        "in 999999999 hours" parses to a timestamp `datetime.fromtimestamp`
        cannot render at all, and no `at`/`delay` check sees it — the resolved
        instant has to carry the ceiling for the crash class to be closed.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request(
                {"name": "forever", "message": "ping", "at_time": "in 999999999 hours"}, crons
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "at_out_of_range"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_bounds_are_read_from_the_schema_not_restated(self):
        """The route's bounds must BE the tool's bounds, not a copy of them.

        Asserted against `CRON_ADD_SCHEMA` itself so retuning the schema cannot
        silently leave this route on the old numbers — the divergence the route
        exists to close.
        """
        specs = {f.name: f for f in CRON_ADD_SCHEMA.fields}
        at_max = specs["at"].max_val
        delay_max = specs["delay"].max_val
        assert at_max is not None and delay_max is not None
        crons = CronService()
        # One tick past each ceiling is refused; the ceiling itself is not,
        # unless it is already in the past (`at`'s ceiling is a fixed epoch).
        resp = await api_crons_create(
            _create_request({"name": "edge", "message": "ping", "at": at_max + 1}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at"
        resp = await api_crons_create(
            _create_request({"name": "edge2", "message": "ping", "delay": delay_max + 1}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_delay"
        resp = await api_crons_create(
            _create_request({"name": "ok", "message": "ping", "delay": delay_max}, crons)
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_oversized_integer_at_is_refused(self):
        """A JSON integer has no width limit, so `float()` can overflow.

        `float(10**308)` raises `OverflowError`; uncaught that is a bare 500 on
        `POST /api/crons`. Refused as out-of-range instead, since a value
        `float` cannot hold is by definition past any declared ceiling.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "huge", "message": "ping", "at": int("1" + "0" * 308)}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_oversized_integer_delay_is_refused(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request(
                {"name": "huge2", "message": "ping", "delay": int("1" + "0" * 489)}, crons
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_delay"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_overlong_at_time_is_refused_at_the_schema_length(self):
        """`at_time`'s length limit must be the schema's, not the generic one.

        A string longer than the tool accepts is how an oversized relative
        duration reaches the parser, where `time.time() + secs` on a
        several-hundred-digit int overflows into a 500. Asserted against
        `CRON_ADD_SCHEMA` so the generic `MAX_SHORT_STRING` cannot creep back.
        """
        specs = {f.name: f for f in CRON_ADD_SCHEMA.fields}
        at_time_max = specs["at_time"].max_len
        assert at_time_max and at_time_max < MAX_SHORT_STRING
        crons = CronService()
        resp = await api_crons_create(
            _create_request(
                {"name": "verbose", "message": "ping", "at_time": "in " + "9" * 490 + " hours"},
                crons,
            )
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at_time"
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_unparseable_at_time_is_refused(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "gibberish", "message": "ping", "at_time": "elevenses"}, crons)
        )
        assert resp.status == 400
        body = _body(resp)
        assert body["code"] == "invalid_at_time"
        # The "Error: " prefix belongs to the parser's string return, not to a
        # JSON body that already labels the field `error`.
        assert not body["error"].startswith("Error: ")

    @pytest.mark.asyncio
    async def test_non_numeric_at_is_refused(self):
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "stringly", "message": "ping", "at": "5pm"}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at"

    @pytest.mark.asyncio
    async def test_bool_at_is_refused_not_read_as_one(self):
        """`isinstance(True, int)` is true in Python, so bools need their own gate.

        Without it `{"at": true}` resolves to epoch 1 — a past timestamp whose
        refusal would name the wrong problem.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "boolish", "message": "ping", "at": True}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_at"

    @pytest.mark.asyncio
    async def test_nan_delay_is_refused(self):
        """`json.loads` accepts NaN, and NaN defeats the past-time comparison.

        Every comparison against NaN is false, so an unguarded NaN would persist
        a job whose next run can never arrive.
        """
        crons = CronService()
        resp = await api_crons_create(
            _create_request({"name": "nan", "message": "ping", "delay": float("nan")}, crons)
        )
        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_delay"
        assert crons.list_jobs() == []
