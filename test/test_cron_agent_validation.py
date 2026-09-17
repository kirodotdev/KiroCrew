"""Save-time agent validation on the cron REST surface.

A scheduled job's agent is validated TWICE: once when the job is saved (here) and
again at every fire (``slack/gateway.py``, both cron paths). The pair is what makes
"unresolvable means skip, never fall back" true -- save-time refusal gives the
operator the error while the form is still open, and the fire-time re-check catches
an agent that was valid at save and removed afterwards.

Three shapes stay deliberately valid and are pinned here, because each one looks
like an invalid name to a naive membership test:

* an EMPTY agent, which means "the default";
* a MEMBER-BOUND job, whose ``agent_id`` is the crew member's provider template
  rather than a selectable name;
* an edit that does not move the binding, so a job whose agent was deleted can
  still be renamed or repaired instead of being locked by the very staleness the
  edit is trying to fix.

Validation deliberately goes through the RESOLVER rather than the ``/api/agents``
roster listing: that listing omits app-registered agents, which are dispatchable.

``resolve_agent_bindings`` is patched at ``dashboard.handlers.cron``, NOT at
``config.loader``: the handler imports it at MODULE level, so it holds its own
reference bound at import time and patching the defining module would not reach
it. Patching the loader silently no-ops here and the assertion fails on a real
resolution instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create

UNKNOWN_AGENT = "ghost-agent-that-is-not-configured"


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _request(body: dict, crons: CronService, job_id: str | None = None) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    if job_id is not None:
        request.match_info = {"job_id": job_id}
    return request


def _resolves(ok: bool):
    """Patch the resolver to a fixed verdict, so these tests exercise the HANDLER.

    Loading a real agent roster here would make the assertions depend on whatever
    the host's config happens to contain.
    """
    return patch(
        "kiro_crew.dashboard.handlers.cron.resolve_agent_bindings",
        return_value=MagicMock(requested_resolved=ok),
    )


class TestCreateValidatesTheAgent:
    @pytest.mark.asyncio
    async def test_an_unresolvable_agent_is_refused(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        with _resolves(False):
            resp = await api_crons_create(
                _request(
                    {"name": "n", "message": "m", "every": 3600, "agent": UNKNOWN_AGENT}, crons
                )
            )
        assert resp.status == 400
        assert b"unknown_agent" in resp.body
        # Refused BEFORE add_job, so no orphaned job a retried create would duplicate.
        assert crons.list_jobs() == []

    @pytest.mark.asyncio
    async def test_a_resolvable_agent_is_accepted(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        with _resolves(True):
            resp = await api_crons_create(
                _request({"name": "n", "message": "m", "every": 3600, "agent": "researcher"}, crons)
            )
        assert resp.status == 200
        assert crons.list_jobs()[0].agent_id == "researcher"

    @pytest.mark.asyncio
    async def test_an_empty_agent_means_the_default_and_is_never_probed(self, tmp_path):
        # "" is the default-agent sentinel, so it must not be validated at all --
        # a probe here would be both wrong and a needless config read.
        crons = CronService(base_dir=tmp_path)
        with patch(
            "kiro_crew.dashboard.handlers.cron.resolve_agent_bindings",
            side_effect=AssertionError("probed an empty agent"),
        ):
            resp = await api_crons_create(
                _request({"name": "n", "message": "m", "every": 3600, "agent": ""}, crons)
            )
        assert resp.status == 200
        assert crons.list_jobs()[0].agent_id == ""

    @pytest.mark.asyncio
    async def test_a_probe_failure_does_not_block_the_save(self, tmp_path):
        # The fire-time check is the guarantee; refusing a legitimate save because
        # a config read transiently failed is the worse failure mode.
        crons = CronService(base_dir=tmp_path)
        with patch(
            "kiro_crew.dashboard.handlers.cron.resolve_agent_bindings",
            side_effect=RuntimeError("config unreadable"),
        ):
            resp = await api_crons_create(
                _request({"name": "n", "message": "m", "every": 3600, "agent": "researcher"}, crons)
            )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_member_bound_job_is_not_agent_validated(self, tmp_path):
        # A member-bound job's agent_id is the crew member's PROVIDER TEMPLATE, not
        # a selectable agent name, and the member itself is validated against
        # config.agents by resolve_cron_memory before the job can persist. Probing
        # the template here would refuse a legitimate member-bound schedule.
        #
        # The assertion is that the AGENT probe never runs: the patched resolver
        # raises if it is reached, so a clean response (rather than that
        # AssertionError surfacing) is the proof. This request is still refused --
        # by the unrelated, pre-existing unknown-Crew-Member check -- so the
        # meaningful part is that it is NOT refused as an unknown agent.
        crons = CronService(base_dir=tmp_path)
        with patch(
            "kiro_crew.dashboard.handlers.cron.resolve_agent_bindings",
            side_effect=AssertionError("probed a member-bound job"),
        ):
            resp = await api_crons_create(
                _request(
                    {
                        "name": "n",
                        "message": "m",
                        "every": 3600,
                        "agent": "kirocrew",
                        "member_id": "dev",
                    },
                    crons,
                )
            )
        assert b"unknown_agent" not in resp.body
        assert crons.list_jobs() == []


class TestUpdateValidatesTheAgent:
    @pytest.mark.asyncio
    async def test_switching_to_an_unresolvable_agent_is_refused(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id="researcher")
        with _resolves(False):
            resp = await api_cron_update(_request({"agent": UNKNOWN_AGENT}, crons, job.id))
        assert resp.status == 400
        assert b"unknown_agent" in resp.body
        assert crons.list_jobs()[0].agent_id == "researcher"

    @pytest.mark.asyncio
    async def test_an_edit_that_does_not_move_the_binding_is_not_validated(self, tmp_path):
        # The trap this avoids: a job whose agent was deleted out from under it
        # must stay editable. If every edit re-validated, renaming it -- or
        # clearing the stale agent -- would be refused by the very staleness the
        # edit is trying to repair.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id=UNKNOWN_AGENT)
        with patch(
            "kiro_crew.dashboard.handlers.cron.resolve_agent_bindings",
            side_effect=AssertionError("probed an unrelated edit"),
        ):
            resp = await api_cron_update(_request({"name": "renamed"}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].name == "renamed"

    @pytest.mark.asyncio
    async def test_an_unchanged_stale_agent_does_not_block_a_rename(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id=UNKNOWN_AGENT)
        with _resolves(False):
            resp = await api_cron_update(
                _request({"name": "renamed", "agent": UNKNOWN_AGENT}, crons, job.id)
            )
        assert resp.status == 200
        assert crons.list_jobs()[0].name == "renamed"
        assert crons.list_jobs()[0].agent_id == UNKNOWN_AGENT

    @pytest.mark.asyncio
    async def test_clearing_a_stale_agent_is_allowed(self, tmp_path):
        # The repair path itself: clearing to "" falls back to the default, which
        # always resolves, so a stale job can always be rescued.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id=UNKNOWN_AGENT)
        resp = await api_cron_update(_request({"agent": ""}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].agent_id == ""

    @pytest.mark.asyncio
    async def test_changing_only_the_folder_revalidates_the_stored_agent(self, tmp_path):
        # A project agent exists only inside its folder, so moving the folder can
        # invalidate an agent the job already had -- the binding moved even though
        # the name did not.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id="repobot")
        with _resolves(False):
            with patch(
                "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
                lambda request: True,
            ):
                resp = await api_cron_update(
                    _request({"project_path": str(tmp_path)}, crons, job.id)
                )
        assert resp.status == 400
        assert b"unknown_agent" in resp.body
        assert crons.list_jobs()[0].project_path == ""

    @pytest.mark.asyncio
    async def test_a_tilde_folder_is_resolved_before_the_probe(self, tmp_path, monkeypatch):
        """The probe resolves the folder the same way the store and the roster do.

        ``~/proj`` is a valid ``project_path`` (the store expands and realpaths it
        on persist, and ``GET /api/agents?project_path=`` resolves it through the
        same ``resolve_project_path``). A probe handed the RAW string builds a
        relative ``~/proj/.kiro/agents`` -- a directory literally named ``~`` --
        and refuses a project agent the picker just listed. HOME is a temp dir so
        the tilde expands under the test, never into the real home.
        """
        import os

        # ``expanduser`` reads USERPROFILE on Windows and HOME on POSIX, so both
        # are set: with only HOME the tilde expands into the real profile there
        # and the folder under it does not exist.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        (tmp_path / "proj").mkdir()
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id="repobot")
        seen: list = []

        def _spy(cfg, agent, project_dir, **kwargs):
            seen.append(project_dir)
            return MagicMock(requested_resolved=True)

        with patch("kiro_crew.dashboard.handlers.cron.resolve_agent_bindings", _spy):
            with patch(
                "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
                lambda request: True,
            ):
                resp = await api_cron_update(_request({"project_path": "~/proj"}, crons, job.id))
        assert resp.status == 200, resp.body
        assert seen == [
            os.path.realpath(str(tmp_path / "proj"))
        ], "the save-time probe resolved the agent against the raw, unexpanded folder"
        assert crons.list_jobs()[0].project_path == seen[0]
