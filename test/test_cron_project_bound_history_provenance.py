"""Project-bound cron output is withheld from a non-owner by PERSISTED provenance.

A project-bound run's retained ``summary``/``trace`` must stay withheld from a
non-owner even after the owner clears the binding or deletes the job, because
history rows outlive both -- see ``cron_history.py``'s per-``job_id`` JSONL
file. Deciding that from the live job's ``project_path`` cannot express it, so
``CronRunRecord.project_bound`` carries the decision instead, stamped at the
three ``cron.py`` write sites from the job that actually fired the run.

``api_cron_to_chat`` (handlers/cron.py) is the one serving route with no field
for the redaction pass to intercept -- it injects a retained result, or a
deleted job's replayed transcript, straight into a shared dashboard chat slot
-- so a non-owner is refused there outright.

These tests pin the provenance write, all four history-serialization read
sites (list, paginated history, run detail, unified all-history), and the
``to-chat`` owner gate -- including its deleted-job path, which asks the run's
own history file because no live job is there to answer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import ANY, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard.handlers.cron import (
    api_cron_history,
    api_cron_history_all,
    api_cron_history_detail,
    api_cron_to_chat,
)

_OWNER_GATE = "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request"
_INJECT = "kiro_crew.dashboard.handlers.cron.inject_cron_result_to_dashboard"

# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_cron_store(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


@pytest.fixture
def crons(tmp_path):
    return CronService(base_dir=tmp_path)


@pytest.fixture
def unbound_job(crons):
    return crons.add_job(name="n", message="m", every_secs=3600)


@pytest.fixture
def bound_job(crons, tmp_path):
    return crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))


@pytest.fixture
def mock_inject():
    with patch(_INJECT) as mock:
        yield mock


def _owner_is(owner: bool):
    return patch(_OWNER_GATE, lambda request: owner)


def _history_app(handler, path):
    app = web.Application()
    app.router.add_get(path, handler)
    return app


class _Read:
    """A response whose body is already in hand.

    The payload MUST be read while the client is still open: returning the live
    ``ClientResponse`` hands the caller a stream whose connection the
    ``async with`` block has closed, so ``.json()`` raises
    ``ClientConnectionError`` after the server already wrote the body.
    """

    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


async def _history_request(crons: CronService, path: str, handler, route: str, *, owner: bool):
    app = _history_app(handler, route)
    app["state"] = MagicMock(crons=crons)
    with _owner_is(owner):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            return _Read(resp.status, await resp.json())


def _job_history(crons, job_id, *, owner):
    route = "/api/crons/{job_id}/history"
    return _history_request(
        crons, f"/api/crons/{job_id}/history", api_cron_history, route, owner=owner
    )


def _run_detail(crons, job_id, run_id, *, owner):
    route = "/api/crons/{job_id}/history/{run_id}"
    path = f"/api/crons/{job_id}/history/{run_id}"
    return _history_request(crons, path, api_cron_history_detail, route, owner=owner)


def _all_history(crons, *, owner):
    route = "/api/crons/history"
    return _history_request(crons, route, api_cron_history_all, route, owner=owner)


# ── B1: provenance write (cron.py) ──────────────────────────────────────────


class TestProjectBoundStampedAtWriteTime:
    """CronRunRecord.project_bound is stamped from the job that fired, not
    re-derived later -- these tests write records the way each of the three
    cron.py sites does and confirm the field round-trips through the on-disk
    JSONL both ways.
    """

    @pytest.mark.asyncio
    async def test_bound_run_persists_project_bound_true(self, crons):
        history = crons.get_history()
        await history.append(
            CronRunRecord(
                job_id="j1",
                status="success",
                summary="agent read /private/proj/secret.txt",
                trace="agent read /private/proj/secret.txt",
                project_bound=True,
            )
        )
        runs, total = await history.get_job_history("j1")
        assert total == 1
        assert runs[0]["project_bound"] is True

    @pytest.mark.asyncio
    async def test_unbound_run_persists_project_bound_false(self, crons):
        history = crons.get_history()
        await history.append(
            CronRunRecord(job_id="j2", status="success", summary="ok", project_bound=False)
        )
        runs, _total = await history.get_job_history("j2")
        assert runs[0]["project_bound"] is False

    @pytest.mark.asyncio
    async def test_legacy_row_with_no_project_bound_key_omits_it(self, tmp_path, crons):
        """A row written before this field existed has no key at all -- proving
        the backfill decision (treat-as-bound) is a READ-time policy, not
        something this write path retroactively injects.
        """
        history = crons.get_history()
        job_path = tmp_path / "cron-history" / "j3.jsonl"
        job_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        job_path.write_text(json.dumps({"run_id": "r1", "job_id": "j3", "summary": "old"}) + "\n")
        # Rebuild the index line too, since get_job_history reads the per-job
        # file directly and does not need it, but get_all_history does.
        runs, _total = await history.get_job_history("j3")
        assert "project_bound" not in runs[0]

    def test_every_cron_run_record_site_stamps_project_bound(self):
        """Every ``CronRunRecord(...)`` construction in cron.py passes
        ``project_bound`` explicitly.

        The field's own contract claims it is "stamped once, at write time, by
        the one caller that knows what cwd the run used (``cron.py``'s three
        ``CronRunRecord(...)`` sites)". A site that omits it silently takes the
        ``False`` default, so a project-bound run's text is served to a
        non-owner path-stripped only -- and no behavioural test catches that,
        because the tests above construct records directly rather than through
        the production sites. The cancel and reaper sites both shipped that way.

        Asserted over the call sites rather than a count, so a FOURTH site added
        later is held to the same rule instead of merely changing a number.
        """
        import ast as _ast
        from pathlib import Path as _Path

        import kiro_crew.cron as _cron

        tree = _ast.parse(_Path(_cron.__file__).read_text(encoding="utf-8"))
        sites = [
            node
            for node in _ast.walk(tree)
            if isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Name)
            and node.func.id == "CronRunRecord"
        ]
        assert sites, "no CronRunRecord(...) construction found in cron.py"
        unstamped = [
            node.lineno
            for node in sites
            if not any(kw.arg == "project_bound" for kw in node.keywords)
        ]
        assert not unstamped, (
            f"CronRunRecord(...) at cron.py line(s) {unstamped} omit project_bound, "
            "so those rows default to False and are served to a non-owner "
            "path-stripped only. Stamp project_bound=bool(job.project_path)."
        )


# ── B1: all four history read sites withhold on the RUN's own provenance ───


class TestHistoryRoutesWithholdOnPersistedProvenance:
    @pytest.mark.asyncio
    async def test_paginated_history_withholds_bound_row_after_job_unbound(self, crons, bound_job):
        """The defect this closes: job.project_path cleared AFTER a bound run
        wrote its row. The OLD code re-derived _withhold from the live job and
        would have served this row unredacted to a non-owner.
        """
        await crons.get_history().append(
            CronRunRecord(
                job_id=bound_job.id,
                status="success",
                summary="agent read /private/proj/secret.txt",
                project_bound=True,
            )
        )
        # Owner clears the binding on the LIVE job -- the row already on disk
        # keeps its own stamp.
        bound_job.project_path = ""

        resp = await _job_history(crons, bound_job.id, owner=False)
        assert resp.status == 200
        body = await resp.json()
        assert body["runs"][0]["summary"] == ""

    @pytest.mark.asyncio
    async def test_paginated_history_owner_still_sees_bound_row(self, crons, bound_job):
        await crons.get_history().append(
            CronRunRecord(
                job_id=bound_job.id, status="success", summary="secret", project_bound=True
            )
        )
        bound_job.project_path = ""
        resp = await _job_history(crons, bound_job.id, owner=True)
        body = await resp.json()
        assert body["runs"][0]["summary"] == "secret"

    @pytest.mark.asyncio
    async def test_paginated_history_never_bound_row_unaffected_for_non_owner(
        self, crons, unbound_job
    ):
        """MANDATORY negative pin: a run that was never project-bound must read
        identically for owner and non-owner (only path-strip, never withhold).
        """
        await crons.get_history().append(
            CronRunRecord(
                job_id=unbound_job.id, status="success", summary="plain result", project_bound=False
            )
        )
        resp = await _job_history(crons, unbound_job.id, owner=False)
        body = await resp.json()
        assert body["runs"][0]["summary"] == "plain result"

    @pytest.mark.asyncio
    async def test_history_detail_withholds_bound_row_after_job_deleted(self, crons, bound_job):
        """The row's own job is absent at read time -- get_run_detail has
        no live job to consult at all, so this pins that the withhold decision
        comes from the row, not a (missing) job lookup.
        """
        record = CronRunRecord(
            job_id=bound_job.id,
            status="success",
            summary="agent read /private/proj/secret.txt",
            trace="agent read /private/proj/secret.txt",
            project_bound=True,
        )
        await crons.get_history().append(record)
        crons.remove_job(bound_job.id, actor="test", source="test")

        resp = await _run_detail(crons, bound_job.id, record.run_id, owner=False)
        assert resp.status == 200
        body = await resp.json()
        assert body["summary"] == ""
        assert body["trace"] == ""

    @pytest.mark.asyncio
    async def test_history_detail_owner_sees_bound_row_after_job_deleted(self, crons, bound_job):
        record = CronRunRecord(
            job_id=bound_job.id,
            status="success",
            summary="secret",
            trace="secret",
            project_bound=True,
        )
        await crons.get_history().append(record)
        crons.remove_job(bound_job.id, actor="test", source="test")

        resp = await _run_detail(crons, bound_job.id, record.run_id, owner=True)
        body = await resp.json()
        assert body["summary"] == "secret"

    @pytest.mark.asyncio
    async def test_history_detail_legacy_row_reads_as_not_bound_for_non_owner(
        self, tmp_path, crons, unbound_job
    ):
        """Backfill decision: an absent project_bound key reads as NOT bound.

        Every row this code writes carries the key (``asdict``), so an absent
        one predates the field -- and ``project_path`` arrives in the same
        change, so such a row cannot have fired project-bound. Withholding it
        would hide all pre-existing history from a legitimate non-owner to
        guard a case that cannot exist; the path strip still applies.
        """
        record = CronRunRecord(job_id=unbound_job.id, status="success", summary="legacy secret")
        # Simulate a pre-migration row: write it, then strip the key from disk
        # the way a record written before the field existed would look.
        await crons.get_history().append(record)
        job_path = tmp_path / "cron-history" / f"{unbound_job.id}.jsonl"
        import json

        lines = job_path.read_text(encoding="utf-8").strip().splitlines()
        rows = [json.loads(line) for line in lines]
        for row in rows:
            row.pop("project_bound", None)
        job_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

        resp = await _run_detail(crons, unbound_job.id, record.run_id, owner=False)
        body = await resp.json()
        assert body["summary"] == "legacy secret"

    @pytest.mark.asyncio
    async def test_all_history_withholds_bound_row_after_job_unbound(self, crons, bound_job):
        await crons.get_history().append(
            CronRunRecord(
                job_id=bound_job.id, status="success", summary="secret", project_bound=True
            )
        )
        bound_job.project_path = ""

        resp = await _all_history(crons, owner=False)
        assert resp.status == 200
        body = await resp.json()
        assert body["runs"][0]["summary"] == ""

    @pytest.mark.asyncio
    async def test_all_history_never_bound_row_unaffected(self, crons, unbound_job):
        await crons.get_history().append(
            CronRunRecord(
                job_id=unbound_job.id, status="success", summary="fine", project_bound=False
            )
        )
        resp = await _all_history(crons, owner=False)
        body = await resp.json()
        assert body["runs"][0]["summary"] == "fine"


# ── B2: to-chat owner gate ───────────────────────────────────────────────────


def _to_chat_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/crons/{job_id}/to-chat", api_cron_to_chat)
    return app


async def _post_to_chat(state, job_id):
    async with TestClient(TestServer(_to_chat_app(state))) as client:
        _r = await client.post(f"/api/crons/{job_id}/to-chat")
        return _Read(_r.status, await _r.json())


def _make_to_chat_state(crons, history_messages=None):
    state = MagicMock()
    slots = {}

    def get_or_create_slot(name=None, agent="", origin=""):
        if name not in slots:
            slot = MagicMock()
            slot.key = name
            slot.linked_session_key = ""
            slot.messages = []
            slot.title = ""

            def append(role, content, cls, broadcast=True, meta=None, mint_mid=True):
                slot.messages.append({"role": role, "content": content, "cls": cls})
                return slot.messages[-1]

            slot.append = append
            slots[name] = slot
        return slots[name]

    state.get_or_create_slot = get_or_create_slot
    state.crons = crons
    state.conversation_log = MagicMock()
    state.conversation_log.read_messages.return_value = history_messages or []
    state._notification_log = []
    state.push_slots_update = MagicMock()
    state.has_slot = MagicMock(return_value=False)
    return state


class TestToChatOwnerGateLiveJob:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_open_a_bound_jobs_result(self, crons, bound_job, mock_inject):
        bound_job.last_result = "agent read /private/proj/secret.txt"
        state = _make_to_chat_state(crons)
        with _owner_is(False):
            resp = await _post_to_chat(state, bound_job.id)
        assert resp.status == 403
        body = await resp.json()
        assert body["code"] == "project_bound_job_owner_required"
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_can_still_open_a_bound_jobs_result(self, crons, bound_job, mock_inject):
        bound_job.last_result = "secret"
        state = _make_to_chat_state(crons)
        with _owner_is(True):
            resp = await _post_to_chat(state, bound_job.id)
        assert resp.status == 200
        mock_inject.assert_called_once_with(
            state, bound_job, "secret", history=ANY, dismissed=ANY, include_prompt=False
        )

    @pytest.mark.asyncio
    async def test_non_owner_can_still_open_an_unbound_jobs_result(
        self, crons, unbound_job, mock_inject
    ):
        """MANDATORY negative pin: a never-bound job is unaffected for a
        non-owner reader -- the gate must not become a blanket to-chat lock.
        """
        unbound_job.last_result = "fine to see"
        # A never-bound job that RAN has a row stamped unbound. Without one the
        # gate sees retained output and no row to read, which is the ambiguous
        # case it now withholds -- so the pin has to describe a real run.
        await crons.get_history().append(
            CronRunRecord(
                job_id=unbound_job.id, status="success", summary="fine to see", project_bound=False
            )
        )
        state = _make_to_chat_state(crons)
        with _owner_is(False):
            resp = await _post_to_chat(state, unbound_job.id)
        assert resp.status == 200
        mock_inject.assert_called_once_with(
            state, unbound_job, "fine to see", history=ANY, dismissed=ANY, include_prompt=False
        )

    @pytest.mark.asyncio
    async def test_a_result_less_agent_run_after_unbinding_does_not_un_withhold(
        self, crons, bound_job
    ):
        """THE CHAIN: a project-bound run succeeds, the owner clears the
        binding, then a result-less AGENT run fires. That run carries the prior
        reply forward -- ``clear_carried_result`` clears only for
        ``command``/``script`` -- and stamps its own history row from the live,
        by-then-unbound job. A newest-row read answers unbound while the bound
        reply is still in ``last_result``, and nothing ever re-withholds it.

        The stamp travels with the text, so the answer does not move when a
        later run writes a row that describes only itself.
        """
        bound_job.set_run_result("agent read /private/proj/secret.txt")
        bound_job.project_path = ""
        # The result-less agent run: ``clear_carried_result`` is reached only for
        # a ``command``/``script`` job, so an agent run never clears the carried
        # reply -- it simply survives, and the run stamps its own row from the
        # now-unbound job.
        assert bound_job.last_result == "agent read /private/proj/secret.txt", (
            "an agent run carries the prior reply forward, which is what makes "
            "the newest-row read wrong"
        )
        await crons.get_history().append(
            CronRunRecord(job_id=bound_job.id, status="success", project_bound=False)
        )
        state = _make_to_chat_state(crons)
        with _owner_is(False):
            resp = await _post_to_chat(state, bound_job.id)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_non_owner_cannot_open_a_result_after_the_owner_clears_the_binding(
        self, crons, bound_job, mock_inject
    ):
        """THE FINDING (GPT 5.6): the owner binds a job, it fires, then the
        owner CLEARS ``project_path`` on the still-LIVE job while the agent's
        reply stays in ``last_result``. The gate must still refuse a non-owner,
        reading the stamp that travelled with the retained text rather than the
        now-empty live binding.

        The history row here deliberately says ``project_bound=False`` -- the
        wrong answer -- so this also pins that the row is NOT the source: a
        later result-less run can stamp an unbound row over a retained bound
        reply, which is the disclosure the travelling stamp closes.
        """
        # Produce the result through the real write path WHILE bound, so the
        # stamp is set the way a live run sets it.
        bound_job.set_run_result("agent read /private/proj/secret.txt")
        await crons.get_history().append(
            CronRunRecord(
                job_id=bound_job.id,
                status="success",
                summary="a later result-less run",
                project_bound=False,
            )
        )
        # Owner clears the binding on the STILL-LIVE job.
        bound_job.project_path = ""
        state = _make_to_chat_state(crons)
        with _owner_is(False):
            resp = await _post_to_chat(state, bound_job.id)
        assert resp.status == 403
        body = await resp.json()
        assert body["code"] == "project_bound_job_owner_required"
        mock_inject.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_still_opens_a_result_after_clearing_the_binding(
        self, crons, bound_job, mock_inject
    ):
        """The owner is never gated: after clearing the binding they still get
        their own retained reply verbatim.
        """
        await crons.get_history().append(
            CronRunRecord(
                job_id=bound_job.id, status="success", summary="secret", project_bound=True
            )
        )
        bound_job.last_result = "secret"
        bound_job.project_path = ""
        state = _make_to_chat_state(crons)
        with _owner_is(True):
            resp = await _post_to_chat(state, bound_job.id)
        assert resp.status == 200
        mock_inject.assert_called_once_with(
            state, bound_job, "secret", history=ANY, dismissed=ANY, include_prompt=False
        )


class TestToChatOwnerGateDeletedJob:
    """The job is gone; the gate has to ask the run's own history file."""

    @pytest.mark.asyncio
    async def test_non_owner_cannot_open_history_of_a_deleted_bound_job(self, crons, bound_job):
        job_id = bound_job.id
        await crons.get_history().append(
            CronRunRecord(job_id=job_id, status="success", summary="secret", project_bound=True)
        )
        crons.remove_job(job_id, actor="test", source="test")

        history_messages = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "agent read /private/proj/secret.txt"},
        ]
        state = _make_to_chat_state(crons, history_messages=history_messages)
        with _owner_is(False):
            resp = await _post_to_chat(state, job_id)
        assert resp.status == 403
        body = await resp.json()
        assert body["code"] == "project_bound_job_owner_required"
        # No slot should have been created/populated with the transcript.
        assert (
            not state.has_slot.called
            or state.get_or_create_slot(name=f"cron-{job_id}").messages == []
        )

    @pytest.mark.asyncio
    async def test_owner_can_open_history_of_a_deleted_bound_job(self, crons, bound_job):
        job_id = bound_job.id
        await crons.get_history().append(
            CronRunRecord(job_id=job_id, status="success", summary="secret", project_bound=True)
        )
        crons.remove_job(job_id, actor="test", source="test")

        history_messages = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "secret result"},
        ]
        state = _make_to_chat_state(crons, history_messages=history_messages)
        with _owner_is(True):
            resp = await _post_to_chat(state, job_id)
        assert resp.status == 200
        slot = state.get_or_create_slot(name=f"cron-{job_id}")
        assert len(slot.messages) == 2

    @pytest.mark.asyncio
    async def test_non_owner_can_open_history_of_a_deleted_never_bound_job(
        self, crons, unbound_job
    ):
        """MANDATORY negative pin, deleted-job branch: a never-bound job's
        history stays reachable for a non-owner after deletion.
        """
        job_id = unbound_job.id
        await crons.get_history().append(
            CronRunRecord(
                job_id=job_id,
                status="success",
                summary="fine",
                started_at=1_700_000_000.0,
                finished_at=1_700_000_010.0,
                project_bound=False,
            )
        )
        crons.remove_job(job_id, actor="test", source="test")

        history_messages = [
            {
                "role": "user",
                "content": "run it",
                "ts": datetime.fromtimestamp(1_700_000_001.0, tz=timezone.utc).isoformat(),
            },
            {
                "role": "assistant",
                "content": "fine result",
                "ts": datetime.fromtimestamp(1_700_000_005.0, tz=timezone.utc).isoformat(),
            },
        ]
        state = _make_to_chat_state(crons, history_messages=history_messages)
        with _owner_is(False):
            resp = await _post_to_chat(state, job_id)
        assert resp.status == 200
        slot = state.get_or_create_slot(name=f"cron-{job_id}")
        assert len(slot.messages) == 2

    @pytest.mark.asyncio
    async def test_non_owner_deleted_job_no_history_row_is_withheld(self, crons):
        """A deleted job with NO history row cannot prove it ran UNBOUND either,
        and the fallback it would reach is the notification body -- which carries
        the reply the run produced and is only path/credential-redacted, never
        withheld. With the job gone there is no live ``project_path`` to consult,
        so "cannot tell" has to mean withhold: a non-owner losing an ordinary
        status line is a smaller loss than one reading a transcript composed
        inside a private directory. A deleted job that stamped itself unbound
        still serves, which is the positive control above.
        """
        state = _make_to_chat_state(crons)
        state._notification_log = [{"job_id": "ghost1", "body": "Cron completed"}]
        with _owner_is(False):
            resp = await _post_to_chat(state, "ghost1")
        assert resp.status == 403


class TestRetainedResultProvenanceSurvivesRestart:
    """``last_result`` is persisted, so the stamp describing it has to be too.

    A stamp that lives only in memory answers "not bound" for every retained
    project-bound reply after a gateway restart, which re-opens the disclosure
    the stamp exists to close — and nothing re-withholds it afterwards.
    """

    @staticmethod
    def _round_trip(job):
        from kiro_crew.cron_service.store import _job_from_record, job_record

        return _job_from_record(job_record(job))

    def test_a_bound_retained_result_is_still_bound_after_a_round_trip(self, crons, bound_job):
        bound_job.set_run_result("agent read /private/proj/secret.txt")
        assert bound_job.last_result_project_bound is True
        bound_job.project_path = ""

        restored = self._round_trip(bound_job)

        assert restored.last_result == "agent read /private/proj/secret.txt"
        assert (
            restored.last_result_project_bound is True
        ), "the stamp must round-trip, or a restart serves the retained bound reply"

    def test_a_record_written_before_the_field_existed_reads_unbound(self, crons, unbound_job):
        from kiro_crew.cron_service.store import _job_from_record, job_record

        unbound_job.set_run_result("ordinary unbound output")
        record = job_record(unbound_job)
        del record["last_result_project_bound"]

        assert _job_from_record(record).last_result_project_bound is False

    def test_a_present_but_malformed_stamp_withholds(self, crons, unbound_job):
        from kiro_crew.cron_service.store import _job_from_record, job_record

        unbound_job.set_run_result("output")
        record = job_record(unbound_job)
        record["last_result_project_bound"] = "yes"

        assert _job_from_record(record).last_result_project_bound is True

    def test_the_string_false_withholds_rather_than_serving(self, crons, unbound_job):
        """The one malformed value worth naming on its own, because the
        permissive reading of it looks reasonable: a stored ``"false"``.

        It is read as BOUND. Only a real boolean is taken at face value, so
        the string is malformed like any other non-boolean and takes the
        withholding answer. Reading it as "unbound" -- which an ``is True``
        test would -- serves a retained project-bound reply to a non-owner on
        the strength of a value the writer cannot produce (the field is typed
        ``bool`` and serialized straight through), so the only way to store it
        is to edit the record outside this process. A disclosure flag cannot
        take its answer from that.
        """
        from kiro_crew.cron_service.store import _job_from_record, job_record

        unbound_job.set_run_result("output")
        record = job_record(unbound_job)
        record["last_result_project_bound"] = "false"

        assert _job_from_record(record).last_result_project_bound is True

    def test_a_real_boolean_still_round_trips_both_ways(self, crons, unbound_job):
        """The guard must not become a blanket withhold: a genuine ``False``
        is taken at face value, which is what keeps an unbound result readable
        by a non-owner after a restart.
        """
        from kiro_crew.cron_service.store import _job_from_record, job_record

        unbound_job.set_run_result("output")
        record = job_record(unbound_job)
        assert record["last_result_project_bound"] is False
        assert _job_from_record(record).last_result_project_bound is False

        record["last_result_project_bound"] = True
        assert _job_from_record(record).last_result_project_bound is True


class TestToChatTranscriptIsFilteredPerRun:
    """The replay is CUMULATIVE while the result gate answers for the LATEST
    result only, so a bound run followed by an unbound one passes that gate
    with the bound run's output still in the transcript. The rows carry no
    provenance and none can be back-filled, so it is DERIVED from each run
    record's own window. Fail-closed: a row is served only when this read can
    PROVE it belongs to an unbound run.
    """

    T0 = 1_700_000_000.0

    @staticmethod
    def _row(epoch: float, content: str) -> dict:
        ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        return {"role": "assistant", "content": content, "ts": ts}

    @staticmethod
    async def _run(job_id: str, rows: list[dict], runs: list[CronRunRecord], crons, *, owner=False):
        """Drive to-chat once; answer (status, served contents or None)."""
        for record in runs:
            await crons.get_history().append(record)
        state = _make_to_chat_state(crons, history_messages=rows)
        state._notification_log = [{"job_id": job_id, "body": "agent read /private/proj/secret"}]
        with (
            _owner_is(owner),
            patch(_INJECT) as mock_inject,
        ):
            async with TestClient(TestServer(_to_chat_app(state))) as client:
                resp = await client.post(f"/api/crons/{job_id}/to-chat")
                status = resp.status
        if not mock_inject.called:
            return status, None
        return status, [m["content"] for m in mock_inject.call_args.kwargs["history"]]

    @classmethod
    def _bound(cls, job_id, *, started=None, finished=None):
        return CronRunRecord(
            job_id=job_id,
            status="success",
            summary="bound",
            started_at=cls.T0 if started is None else started,
            finished_at=cls.T0 + 10 if finished is None else finished,
            project_bound=True,
        )

    @classmethod
    def _unbound(cls, job_id, *, started=None, finished=None):
        return CronRunRecord(
            job_id=job_id,
            status="success",
            summary="unbound",
            started_at=cls.T0 + 100 if started is None else started,
            finished_at=cls.T0 + 110 if finished is None else finished,
            project_bound=False,
        )

    @pytest.mark.asyncio
    async def test_bound_rows_are_withheld_while_unbound_rows_are_served(self, crons, unbound_job):
        """GPT's exact chain: bound fire, owner clears the binding, a later
        unbound run re-stamps the retained result, non-owner to-chat. The
        latest-result gate PASSES here by design -- the bound text is in the
        replayed transcript, not in ``last_result``.
        """
        unbound_job.last_result = "unbound latest"
        rows = [
            self._row(self.T0 + 5, "agent read /private/proj/secret.txt"),
            self._row(self.T0 + 105, "public ok"),
        ]
        runs = [self._bound(unbound_job.id), self._unbound(unbound_job.id)]
        status, served = await self._run(unbound_job.id, rows, runs, crons)
        assert status == 200
        assert served == ["public ok"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", [True, "false", None, [], {}])
    async def test_explicit_or_malformed_row_provenance_overrides_unbound_window(
        self, crons, unbound_job, marker
    ):
        unbound_job.last_result = "unbound latest"
        row = self._row(self.T0 + 105, "retained bound result")
        row["meta"] = {"project_bound": marker}

        status, served = await self._run(
            unbound_job.id, [row], [self._unbound(unbound_job.id)], crons
        )

        assert status == 200
        assert served == []

    @pytest.mark.asyncio
    async def test_the_owner_still_sees_the_whole_transcript(self, crons, unbound_job):
        unbound_job.last_result = "unbound latest"
        rows = [self._row(self.T0 + 5, "bound text"), self._row(self.T0 + 105, "public ok")]
        runs = [self._bound(unbound_job.id), self._unbound(unbound_job.id)]
        status, served = await self._run(unbound_job.id, rows, runs, crons, owner=True)
        assert status == 200
        assert served == ["bound text", "public ok"]

    @pytest.mark.asyncio
    async def test_a_never_bound_job_serves_only_retained_unbound_windows(self, crons, unbound_job):
        """Rows outside retained run windows have unknown provenance.

        Even when every retained record is unbound, an older bound record may
        have been pruned while its transcript rows survive. Only rows positively
        covered by an unbound window are safe to replay.
        """
        unbound_job.last_result = "fine"
        rows = [self._row(self.T0 + 105, "in window"), self._row(self.T0 + 5000, "unknown")]
        status, served = await self._run(
            unbound_job.id, rows, [self._unbound(unbound_job.id)], crons
        )
        assert status == 200
        assert served == ["in window"]

    @pytest.mark.asyncio
    async def test_pruned_bound_record_does_not_release_its_surviving_transcript(
        self, crons, unbound_job
    ):
        """The 101st record prunes the bound run, not its transcript rows."""
        unbound_job.last_result = "latest unbound result"
        runs = [self._bound(unbound_job.id)]
        for index in range(100):
            started = self.T0 + 100 + index * 20
            runs.append(self._unbound(unbound_job.id, started=started, finished=started + 10))
        rows = [
            self._row(self.T0 + 5, "private row whose bound record is pruned"),
            self._row(self.T0 + 100 + 99 * 20 + 5, "retained unbound row"),
        ]
        status, served = await self._run(unbound_job.id, rows, runs, crons)
        assert status == 200
        assert served == ["retained unbound row"]

    @pytest.mark.asyncio
    async def test_a_row_in_no_window_or_with_an_unreadable_stamp_is_withheld(
        self, crons, unbound_job
    ):
        """Three fail-closed cases on a job that HAS a bound run: a row no run
        accounts for, a row with no stamp, and a stamp that will not parse.
        """
        unbound_job.last_result = "unbound latest"
        rows = [
            self._row(self.T0 + 50, "between runs"),
            {"role": "assistant", "content": "no stamp at all"},
            {"role": "assistant", "content": "unparseable", "ts": "not-a-date"},
            self._row(self.T0 + 105, "public ok"),
        ]
        runs = [self._bound(unbound_job.id), self._unbound(unbound_job.id)]
        status, served = await self._run(unbound_job.id, rows, runs, crons)
        assert status == 200
        assert served == ["public ok"]

    @pytest.mark.asyncio
    async def test_an_empty_run_read_withholds_the_whole_transcript(self, crons, unbound_job):
        """No run record is not evidence of a job that never ran bound: the
        records are capped per job and the read degrades to empty on an
        unreadable store, while the transcript survives either way.
        """
        unbound_job.last_result = "unbound latest"
        rows = [self._row(self.T0 + 5, "could be anything")]
        status, served = await self._run(unbound_job.id, rows, [], crons)
        assert status == 200
        assert served == []

    @pytest.mark.asyncio
    async def test_a_bound_run_with_no_placeable_window_withholds_everything(
        self, crons, unbound_job
    ):
        """A bound run whose ``started_at`` is absent could account for ANY
        row, so nothing is served rather than everything outside a window it
        cannot state.
        """
        unbound_job.last_result = "unbound latest"
        runs = [
            self._bound(unbound_job.id, started=0.0, finished=0.0),
            self._unbound(unbound_job.id),
        ]
        status, served = await self._run(
            unbound_job.id, [self._row(self.T0 + 105, "looks unbound")], runs, crons
        )
        assert status == 200
        assert served == []

    @pytest.mark.asyncio
    async def test_an_unfinished_bound_run_is_open_ended(self, crons, unbound_job):
        """A bound run still in flight has no finish on record, so its output
        is still arriving: everything at or after its start is withheld rather
        than treated as an empty window.
        """
        unbound_job.last_result = "unbound latest"
        runs = [
            self._unbound(unbound_job.id, started=self.T0, finished=self.T0 + 10),
            self._bound(unbound_job.id, started=self.T0 + 100, finished=0.0),
        ]
        rows = [
            self._row(self.T0 + 5, "earlier unbound"),
            self._row(self.T0 + 900, "after the bound start"),
        ]
        status, served = await self._run(unbound_job.id, rows, runs, crons)
        assert status == 200
        assert served == ["earlier unbound"]

    @pytest.mark.asyncio
    async def test_a_deleted_jobs_notification_body_is_withheld_once_a_run_was_bound(self, crons):
        """With the job gone the notification body has no provenance of its own
        and no live stamp to consult, so it is withheld rather than served as
        the fallback for a transcript this filter just emptied. The newest row
        is unbound, so the gate above LETS THIS THROUGH.
        """
        runs = [self._bound("gone"), self._unbound("gone")]
        rows = [self._row(self.T0 + 5, "bound text")]
        status, served = await self._run("gone", rows, runs, crons)
        assert status == 403
        assert served is None

    @pytest.mark.asyncio
    async def test_an_empty_transcript_does_not_release_a_bound_notification(self, crons):
        """A hide_in_chat job writes no transcript; a bound run's body is still
        the OLDEST note, and the newest run is unbound so the gate passes."""
        for record in (self._bound("hid"), self._unbound("hid")):
            await crons.get_history().append(record)
        state = _make_to_chat_state(crons)
        state._notification_log = [
            {"job_id": "hid", "body": "bound secret", "project_bound": True},
            {"job_id": "hid", "body": "unbound", "project_bound": False},
        ]
        with _owner_is(False):
            resp = await _post_to_chat(state, "hid")
        assert resp.status == 403
        assert state.get_or_create_slot(name="cron-hid").messages == []
        with _owner_is(True):
            resp = await _post_to_chat(state, "hid")
        assert resp.status == 200
        assert [m["content"] for m in state.get_or_create_slot(name="cron-hid").messages] == [
            "unbound"
        ]
