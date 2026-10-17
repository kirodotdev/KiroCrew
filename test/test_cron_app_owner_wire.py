"""The owning app reaches the ``GET /api/crons`` wire, and nothing else does.

A cron job an installed app created is tagged ``created_by = "app:<name>"`` by
:class:`~kiro_crew.apps.cron_sdk.CronSDK`, but the dashboard could not see it: the
listing serializer names its fields one by one and ``created_by`` was not among
them. Without that attribution the dashboard can tell a job is running or has
failed, yet not whose it is.

``created_by`` is shared with a human creator's Slack user ID, so the wire carries
the derived app reading instead of the raw field -- the two cases below pin both
halves of that: an app-owned job reports its app, and a person-owned one reports
nothing rather than leaking the id.

Harness mirrors test_cron_retry_wire.py.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.apps.cron_sdk import APP_OWNER_PREFIX, app_owner_name, owner_tag
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers.cron import api_crons


def _request_with_job(**job_kw) -> MagicMock:
    defaults = dict(
        id="cj1",
        name="nightly",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="echo hello",
        last_status="ok",
    )
    defaults.update(job_kw)
    job = CronJob(**defaults)

    state = MagicMock()
    state.crons.list_jobs.return_value = [job]
    state.crons.list_jobs_async = AsyncMock(return_value=[job])
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    state.has_slot.return_value = False
    request = MagicMock()
    request.app = {"state": state}
    return request


async def _job_payload(**job_kw) -> dict:
    resp = await api_crons(_request_with_job(**job_kw))
    assert resp.status == 200
    return json.loads(resp.body)["jobs"][0]


class TestTheOwningAppOnTheWire:
    @pytest.mark.asyncio
    async def test_an_app_owned_job_reports_its_app(self) -> None:
        payload = await _job_payload(created_by=owner_tag("my-ledger"))
        assert payload["app"] == "my-ledger"

    @pytest.mark.asyncio
    async def test_a_person_owned_job_reports_no_app(self) -> None:
        # The raw value is a Slack user ID. It must not be serialized under any
        # key: the dashboard has no use for it here, and publishing it would be a
        # new disclosure on a polling endpoint.
        payload = await _job_payload(created_by="U01ABCDEF")
        assert payload["app"] is None
        assert "U01ABCDEF" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_a_job_with_no_creator_reports_no_app(self) -> None:
        payload = await _job_payload()
        assert payload["app"] is None

    @pytest.mark.asyncio
    async def test_a_bare_prefix_reports_no_app(self) -> None:
        # Names no app; an empty string would read as app-owned to a consumer and
        # key a map that matches nothing.
        payload = await _job_payload(created_by=APP_OWNER_PREFIX)
        assert payload["app"] is None


class TestAppOwnerName:
    def test_reads_the_app_out_of_an_owner_tag(self) -> None:
        assert app_owner_name(owner_tag("my-ledger")) == "my-ledger"

    def test_a_person_id_is_not_an_app(self) -> None:
        assert app_owner_name("U01ABCDEF") == ""

    def test_empty_and_none_are_not_an_app(self) -> None:
        assert app_owner_name("") == ""
        assert app_owner_name(None) == ""

    def test_a_bare_prefix_names_no_app(self) -> None:
        assert app_owner_name(APP_OWNER_PREFIX) == ""

    def test_an_app_name_containing_the_prefix_survives(self) -> None:
        # Only the FIRST prefix is the marker; the rest is the name verbatim, so a
        # name is never silently rewritten.
        assert app_owner_name(owner_tag("app:odd")) == "app:odd"

    def test_owner_tag_and_app_owner_name_round_trip(self) -> None:
        # The two are each other's inverse, which is what lets the SDK stamp and
        # the serializer read without either restating the prefix.
        for name in ("a", "my-ledger", "constructor", "issue-radar"):
            assert app_owner_name(owner_tag(name)) == name


class TestThePauseReasonOnTheWire:
    """A user pause and an execution auto-pause must be distinguishable.

    Both land as ``enabled=False`` (``record_failure`` sets it alongside
    ``auto_paused``), so ``enabled`` alone cannot tell them apart -- and the
    difference decides whether a failing job counts as a health signal.
    ``unhealthy_jobs_from_disk`` skips only user pauses and keeps auto-paused
    jobs in their own bucket, so the wire has to be able to express that line.
    """

    @pytest.mark.asyncio
    async def test_a_user_paused_job_says_so(self) -> None:
        payload = await _job_payload(enabled=False, user_paused=True)
        assert payload["user_paused"] is True

    @pytest.mark.asyncio
    async def test_an_auto_paused_job_is_not_reported_as_user_paused(self) -> None:
        # The failing case this guards: a job the scheduler gave up on after
        # repeated failures is an app's WORST job, and reading only `enabled`
        # would hide it exactly like a deliberate pause.
        payload = await _job_payload(enabled=False, user_paused=False, auto_paused=True)
        assert payload["enabled"] is False
        assert payload["user_paused"] is False

    @pytest.mark.asyncio
    async def test_a_normal_job_is_not_paused(self) -> None:
        payload = await _job_payload()
        assert payload["user_paused"] is False
