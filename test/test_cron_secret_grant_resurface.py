"""Tests for resurfacing a pending re-approval request when a grant goes stale.

An approved grant is pinned to the code it was approved over, so editing the script
makes every run refuse it. ``resurface_stale_grant`` files a pending request for the
same mapping over the current code, so the Schedule page has something to approve;
the runner flags exactly the refusals re-approval heals (``grant_stale``), and the
script arm calls the resurfacer only for those. The request never displaces an
agent's own different request, is filed once per code revision (a denial is not
re-asked until the code changes again), and yields to an approval or revocation
that landed after the refusal. The shared fixtures live in
``cron_secret_env_helpers.py``.
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cron_secret_env_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _grant_script,
    cron_home_fixture,
)

from kiro_crew.cron import CronService
from kiro_crew.cron_script import (
    GRANT_PIN_STALE_ERROR,
    _secret_env_precheck,
    compute_secret_env_pin,
    delivery_fingerprint,
    run_script_sandboxed,
)
from kiro_crew.cron_service.grant_resurface import resurface_stale_grant
from kiro_crew.secrets import SecretVault

_GRANT = {"MY_TOKEN": "slack-sandbox"}
_SESSION = "dashboard:conftest-slot"
_EDITED_BODY = "def run(ctx):\n    return None\n"


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    sel_obj = MagicMock(name="sel")
    _floor_monkeypatch.setattr("kiro_crew.cron.sel.sel", lambda: sel_obj)
    return sel_obj


def _pin(spec: str, job_id: str, body: bytes, *, domain: str, grant=None) -> str:
    return compute_secret_env_pin(
        spec,
        "",
        "args",
        job_id=job_id,
        grant=dict(grant or _GRANT),
        domain=domain,
        body=body,
        delivery=delivery_fingerprint(_SESSION),
    )


def _approved_job(svc: CronService, cron_home: Any) -> tuple[str, str]:
    """A script job holding an active grant approved over the original body."""
    spec = _grant_script(cron_home)
    job = svc.add_job("j", "args", every_secs=3600, script=spec, session_key=_SESSION)
    body = _script_path(cron_home).read_bytes()
    svc.update_job(
        job.id, secret_env=dict(_GRANT), secret_env_pin=_pin(spec, job.id, body, domain="active")
    )
    return job.id, spec


def _script_path(cron_home: Any):
    return cron_home() / "crons" / "grantee.py"


def _edit_script(cron_home: Any, body: str = _EDITED_BODY) -> bytes:
    _script_path(cron_home).write_bytes(body.encode())
    return body.encode()


def _fresh(svc: CronService, job_id: str):
    job = CronService(base_dir=svc._dir).get_job(job_id)
    assert job is not None
    return job


class TestRunnerFlagsStaleGrant:
    """``run_script_sandboxed`` marks only the refusals re-approval heals."""

    def test_edited_body_is_flagged_grant_stale(self, cron_home):
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        _edit_script(cron_home)
        job = _fresh(svc, jid)
        result = run_script_sandboxed(
            spec,
            jid,
            "args",
            timeout=30,
            secret_env=job.secret_env,
            secret_env_pin=job.secret_env_pin,
            delivery=delivery_fingerprint(_SESSION),
        )
        assert result["status"] == "error"
        assert GRANT_PIN_STALE_ERROR in result["error"]
        assert result.get("grant_stale") is True

    def test_missing_pin_is_flagged_grant_stale(self, cron_home):
        spec = _grant_script(cron_home)
        result = run_script_sandboxed(spec, "job1", timeout=30, secret_env=dict(_GRANT))
        assert result["status"] == "error"
        assert result.get("grant_stale") is True

    def test_missing_vault_secret_is_not_flagged(self, cron_home):
        """A valid pin whose secret is gone is not healed by re-approval."""
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        job = _fresh(svc, jid)
        result = run_script_sandboxed(
            spec,
            jid,
            "args",
            timeout=30,
            secret_env=job.secret_env,
            secret_env_pin=job.secret_env_pin,
            delivery=delivery_fingerprint(_SESSION),
        )
        assert result["status"] == "error"
        assert "grant_stale" not in result


class TestResurfaceStaleGrant:
    @pytest.mark.asyncio
    async def test_files_pending_request_for_the_current_code(self, cron_home, _quiet_sel):
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        new_body = _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin

        assert await resurface_stale_grant(svc, jid, refused_pin) is True
        _quiet_sel.log_api_access.assert_called_once()

        job = _fresh(svc, jid)
        assert job.secret_env_pending == _GRANT
        assert job.secret_env_pending_pin == _pin(spec, jid, new_body, domain="pending")
        assert job.secret_env_pending_ts > 0
        # The active grant is untouched: still refused, nothing granted.
        assert job.secret_env == _GRANT
        assert job.secret_env_pin == refused_pin
        resolved, err = _secret_env_precheck(
            job.secret_env,
            job.secret_env_pin,
            script=spec,
            script_body=new_body,
            message="args",
            job_id=jid,
            delivery=delivery_fingerprint(_SESSION),
        )
        assert resolved == {} and err == GRANT_PIN_STALE_ERROR

    @pytest.mark.asyncio
    async def test_leaves_an_agents_different_request_alone(self, cron_home):
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        new_body = _edit_script(cron_home)
        other = {"OTHER": "another-secret"}
        other_pin = _pin(spec, jid, new_body, domain="pending", grant=other)
        svc.update_job(
            jid,
            secret_env_pending=other,
            secret_env_pending_pin=other_pin,
            secret_env_pending_ts=5.0,
        )

        assert await resurface_stale_grant(svc, jid, _fresh(svc, jid).secret_env_pin) is False

        job = _fresh(svc, jid)
        assert job.secret_env_pending == other
        assert job.secret_env_pending_pin == other_pin
        assert job.secret_env_pending_ts == 5.0

    @pytest.mark.asyncio
    async def test_a_denied_request_is_not_reasked_until_the_code_changes(self, cron_home):
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin
        assert await resurface_stale_grant(svc, jid, refused_pin) is True
        # The operator denies it (the endpoint clears the pending record).
        svc.update_job(jid, secret_env_pending={})

        assert await resurface_stale_grant(svc, jid, refused_pin) is False
        assert _fresh(svc, jid).secret_env_pending == {}

        newer_body = _edit_script(cron_home, "def run(ctx):\n    return 1\n")
        assert await resurface_stale_grant(svc, jid, refused_pin) is True
        job = _fresh(svc, jid)
        assert job.secret_env_pending == _GRANT
        assert job.secret_env_pending_pin == _pin(spec, jid, newer_body, domain="pending")

    @pytest.mark.asyncio
    async def test_replaces_its_own_request_once_the_code_moves_again(self, cron_home):
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin
        assert await resurface_stale_grant(svc, jid, refused_pin) is True
        # Same code: the request already covers it, nothing is re-filed.
        before = _fresh(svc, jid)
        assert await resurface_stale_grant(svc, jid, refused_pin) is False
        assert _fresh(svc, jid).secret_env_pending_ts == before.secret_env_pending_ts

        newer_body = _edit_script(cron_home, "def run(ctx):\n    return 2\n")
        assert await resurface_stale_grant(svc, jid, refused_pin) is True
        assert _fresh(svc, jid).secret_env_pending_pin == _pin(
            spec, jid, newer_body, domain="pending"
        )

    @pytest.mark.asyncio
    async def test_refreshes_a_stale_same_mapping_request_on_an_already_asked_revision(
        self, cron_home
    ):
        """Asked at B; the agent re-requests over C, then reverts to B undecided.

        The pending request is pinned to C, so approving it would be refused as
        ``code_changed``. The once-per-revision marker (B) must not keep the
        resurfacer from repinning that live request to the code that now runs.
        """
        svc = CronService(base_dir=cron_home() / "store")
        jid, spec = _approved_job(svc, cron_home)
        body_b = _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin
        assert await resurface_stale_grant(svc, jid, refused_pin) is True

        body_c = _edit_script(cron_home, "def run(ctx):\n    return 3\n")
        svc.update_job(
            jid,
            secret_env_pending=dict(_GRANT),
            secret_env_pending_pin=_pin(spec, jid, body_c, domain="pending"),
            secret_env_pending_ts=7.0,
        )
        _edit_script(cron_home, body_b.decode())

        assert await resurface_stale_grant(svc, jid, refused_pin) is True
        job = _fresh(svc, jid)
        assert job.secret_env_pending == _GRANT
        assert job.secret_env_pending_pin == _pin(spec, jid, body_b, domain="pending")

    @pytest.mark.asyncio
    async def test_yields_to_a_decision_made_after_the_refusal(self, cron_home):
        """The run refused pin P; a revoke or re-approval since replaced it."""
        svc = CronService(base_dir=cron_home() / "store")
        jid, _spec = _approved_job(svc, cron_home)
        _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin
        svc.update_job(jid, secret_env={})

        assert await resurface_stale_grant(svc, jid, refused_pin) is False
        assert _fresh(svc, jid).secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_a_store_fault_is_swallowed(self, cron_home):
        from kiro_crew.cron import CronStoreBusy

        svc = CronService(base_dir=cron_home() / "store")
        jid, _spec = _approved_job(svc, cron_home)
        _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin
        with patch.object(svc, "update_job_async", AsyncMock(side_effect=CronStoreBusy("busy"))):
            assert await resurface_stale_grant(svc, jid, refused_pin) is False
        assert _fresh(svc, jid).secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_a_job_removed_mid_filing_reports_nothing(self, cron_home, _quiet_sel):
        """The job is deleted while the pin is computed: no request, no audit row."""
        svc = CronService(base_dir=cron_home() / "store")
        jid, _spec = _approved_job(svc, cron_home)
        _edit_script(cron_home)
        refused_pin = _fresh(svc, jid).secret_env_pin
        with patch.object(svc, "update_job_async", AsyncMock(return_value=None)):
            assert await resurface_stale_grant(svc, jid, refused_pin) is False
        _quiet_sel.log_api_access.assert_not_called()

    def test_marker_round_trips_through_the_store(self, cron_home):
        svc = CronService(base_dir=cron_home() / "store")
        jid, _spec = _approved_job(svc, cron_home)
        svc.update_job(jid, secret_env_resurfaced_pin="abc")
        assert _fresh(svc, jid).secret_env_resurfaced_pin == "abc"


class TestResurfacedRequestIsApprovable:
    """The observable outcome: after a code edit the operator can re-approve in place."""

    @pytest.fixture(autouse=True)
    def _owner_view(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.mark.asyncio
    async def test_edit_then_resurface_then_approve_heals_the_grant(self, cron_home):
        from types import SimpleNamespace

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())
        app = web.Application()
        app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda *a, **k: None)
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        # The runner resolves through cron_script's own (patched) home.
        SecretVault(cron_home()).set_sync("slack-sandbox", "xoxb-1")

        jid, spec = _approved_job(svc, cron_home)
        new_body = _edit_script(cron_home)
        assert await resurface_stale_grant(svc, jid, _fresh(svc, jid).secret_env_pin) is True
        pending = _fresh(svc, jid)

        approve = {
            "approve_pending": True,
            "expected_secret_env": pending.secret_env_pending,
            "expected_ts": pending.secret_env_pending_ts,
            "expected_source_sha256": hashlib.sha256(new_body).hexdigest(),
        }
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=approve)
        assert resp.status == 200, await resp.text()

        job = _fresh(svc, jid)
        assert job.secret_env == _GRANT
        assert job.secret_env_pending == {}
        # The approval ends the marker's suppression.
        assert job.secret_env_resurfaced_pin == ""
        resolved, err = _secret_env_precheck(
            job.secret_env,
            job.secret_env_pin,
            script=spec,
            script_body=new_body,
            message="args",
            job_id=jid,
            delivery=delivery_fingerprint(_SESSION),
        )
        assert err is None
        assert resolved == {"MY_TOKEN": "xoxb-1"}

    @pytest.mark.asyncio
    async def test_reverting_to_an_earlier_resurfaced_revision_asks_again(self, cron_home):
        """Revision B is resurfaced and approved, C is approved after it, then
        the code goes back to B. B was never denied, so asking again is owed."""
        from types import SimpleNamespace

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())
        app = web.Application()
        app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda *a, **k: None)
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")

        async def _approve_current(client: Any, jid: str, body: bytes) -> None:
            pending = _fresh(svc, jid)
            resp = await client.put(
                f"/api/crons/{jid}/secrets",
                json={
                    "approve_pending": True,
                    "expected_secret_env": pending.secret_env_pending,
                    "expected_ts": pending.secret_env_pending_ts,
                    "expected_source_sha256": hashlib.sha256(body).hexdigest(),
                },
            )
            assert resp.status == 200, await resp.text()

        jid, _spec = _approved_job(svc, cron_home)
        body_b = _edit_script(cron_home, "def run(ctx):\n    return 'b'\n")
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                assert await resurface_stale_grant(svc, jid, _fresh(svc, jid).secret_env_pin)
                await _approve_current(client, jid, body_b)
                body_c = _edit_script(cron_home, "def run(ctx):\n    return 'c'\n")
                # The agent requests C itself (cron_secret_request's write).
                svc.update_job(
                    jid,
                    secret_env_pending=dict(_GRANT),
                    secret_env_pending_pin=_pin(_spec, jid, body_c, domain="pending"),
                    secret_env_pending_ts=7.0,
                )
                await _approve_current(client, jid, body_c)

        _edit_script(cron_home, "def run(ctx):\n    return 'b'\n")
        assert await resurface_stale_grant(svc, jid, _fresh(svc, jid).secret_env_pin) is True
        assert _fresh(svc, jid).secret_env_pending == _GRANT


class TestScriptArmCallsTheResurfacer:
    """The gateway's script arm resurfaces only on a ``grant_stale`` refusal."""

    @pytest.mark.parametrize(
        "result,expected",
        [
            ({"status": "error", "error": "stale", "grant_stale": True}, True),
            ({"status": "error", "error": "vault missing"}, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_resurfaces_only_a_stale_refusal(self, monkeypatch, result, expected):
        from cron_gateway_integration_helpers import _make_gw, _make_script_job

        from kiro_crew.slack import gateway as gw_mod

        gw = _make_gw()
        gw._running_script_ids = set()
        gw.cron_svc = MagicMock(name="cron_svc")
        gw._alert_cron_failure = AsyncMock()
        resurface = AsyncMock(return_value=True)
        monkeypatch.setattr(gw_mod, "sel", MagicMock(name="sel"))
        monkeypatch.setattr(
            gw_mod, "_await_cron_fire_time_gate", AsyncMock(return_value=(None, False))
        )
        job = _make_script_job(secret_env=dict(_GRANT), secret_env_pin="refused-pin")

        async def _run_while_an_approval_lands(*args: Any, **kwargs: Any) -> dict:
            # The job object is shared with the store: an approval during the
            # run rewrites its pin in place. The resurfacer must still get the
            # pin this run verified.
            job.secret_env_pin = "approved-pin"
            return result

        monkeypatch.setattr(gw_mod, "run_in_cron_pool", _run_while_an_approval_lands)
        monkeypatch.setattr(gw_mod, "resurface_stale_grant", resurface)

        await gw._cron_run_script(job)

        assert job.last_status == "error"
        if expected:
            resurface.assert_awaited_once_with(gw.cron_svc, job.id, "refused-pin")
        else:
            resurface.assert_not_awaited()
