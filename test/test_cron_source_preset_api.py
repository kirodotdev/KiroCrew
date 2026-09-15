"""``source_preset`` over the dashboard's cron REST surface.

Only the dashboard has the Schedule-page template gallery, so ``POST /api/crons``
is the one create surface that accepts ``source_preset`` (the clicked preset id).
It is returned by the list payload so the Schedule page can compare a saved job's
stored prompt against the source template's current prompt, and it is create-only
— ``PATCH`` never accepts it, because provenance is fixed at creation.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons, api_crons_create

pytestmark = pytest.mark.asyncio


def _app(handler, route: str, **store) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(**store),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_route("*", route, handler)
    return app


def _job(**over) -> CronJob:
    fields = {"id": "j1", "name": "Error Digest", "message": "Summarize errors."}
    fields.update(over)
    return CronJob(**fields)


class TestCreate:
    async def test_source_preset_reaches_the_store(self) -> None:
        add = AsyncMock(return_value=_job(source_preset="error-digest"))
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={
                    "name": "Error Digest",
                    "message": "Summarize errors.",
                    "every": 21600,
                    "source_preset": "error-digest",
                    "source_template_prompt": "Summarize errors.",
                },
            )
            assert resp.status == 200
        assert add.await_args.kwargs["source_preset"] == "error-digest"
        # The prompt SNAPSHOT must reach the store too -- it is the operand the
        # attributable compare fixes at save time.
        assert add.await_args.kwargs["source_template_prompt"] == "Summarize errors."

    async def test_absent_field_forwards_empty_string(self) -> None:
        """A blank create carries no template lineage."""
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={"name": "hand made", "message": "do a thing", "every": 3600},
            )
            assert resp.status == 200
        assert add.await_args.kwargs["source_preset"] == ""
        assert add.await_args.kwargs["source_template_prompt"] == ""

    async def test_non_string_source_preset_is_rejected(self) -> None:
        """A JSON array/int in the field is a 400, not a 500 — same table-driven
        validation as every other string field."""
        add = AsyncMock(return_value=_job())
        app = _app(api_crons_create, "/api/crons", add_job_async=add)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/crons",
                json={
                    "name": "x",
                    "message": "y",
                    "every": 3600,
                    "source_preset": ["not", "a", "string"],
                },
            )
            assert resp.status == 400
        add.assert_not_awaited()


class TestList:
    @staticmethod
    def _payload(job: CronJob):
        state = MagicMock()
        state.has_slot.return_value = False
        state.crons.list_jobs.return_value = [job]
        state.crons.list_jobs_async = AsyncMock(return_value=[job])
        state.crons.is_running.return_value = False
        state.crons.running_since.return_value = None
        request = MagicMock()
        request.app = {"state": state}
        return request

    async def test_source_preset_is_returned_for_the_frontend_compare(self) -> None:
        request = self._payload(
            _job(source_preset="error-digest", source_template_prompt="Original.")
        )
        resp = await api_crons(request)
        row = json.loads(resp.body)["jobs"][0]
        assert row["source_preset"] == "error-digest"
        # The snapshot is returned so the frontend can compare it against the
        # live preset prompt (template moved?) rather than the job's message.
        assert row["source_template_prompt"] == "Original."

    async def test_a_job_without_a_preset_reports_null(self) -> None:
        request = self._payload(_job())
        resp = await api_crons(request)
        assert json.loads(resp.body)["jobs"][0]["source_preset"] is None

    async def test_free_text_snapshot_is_redacted(self) -> None:
        # source_template_prompt is a client-settable POST field, so on the GET
        # it must pass through the same redaction as every other free-text
        # field on the dict. An exfiltration-style URL is scrubbed by the
        # redaction pipeline; asserting it does not survive verbatim pins that
        # the field is not exempt from that pipeline.
        # A Bearer/JWT-shaped value that redact_credentials scrubs. Uses the
        # standard public JWT header segment (decodes to {"alg":"HS256",
        # "typ":"JWT"}) with a synthetic payload -- no real secret, so it
        # exercises the pipeline without embedding a live credential.
        redactable = (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.c3ludGhldGlj.ZmFrZQ"
        )
        request = self._payload(
            _job(source_preset="error-digest", source_template_prompt=redactable)
        )
        resp = await api_crons(request)
        returned = json.loads(resp.body)["jobs"][0]["source_template_prompt"]
        assert returned != redactable
        assert "REDACTED" in returned


class TestUpdate:
    async def test_source_preset_is_not_forwarded_on_patch(self) -> None:
        """Create-only: even if a client sends it, PATCH must not pass it to the
        store (provenance is fixed at creation)."""
        update = AsyncMock(return_value=_job(source_preset="error-digest"))
        app = _app(api_cron_update, "/api/crons/{job_id}", update_job_async=update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/crons/j1", json={"name": "renamed", "source_preset": "standup-brief"}
            )
            assert resp.status == 200
        assert "source_preset" not in update.await_args.kwargs
