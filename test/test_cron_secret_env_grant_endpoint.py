"""Tests for the owner-only endpoint that approves, denies and revokes cron secret grants.

``api_cron_secret_grant`` (``PUT /api/crons/{job_id}/secrets``) promotes a pending request
only when the approval restates its mapping, timestamp and reviewed source digest, and
consumes the request in the same store write. It refuses a direct grant, a decision that
restates a replaced request, and an approval of source that the operator did not load, that
changed since the request or that the display cannot show verbatim. Approve, deny and revoke
share the audit-or-deny gate; a failure in the grant-epoch state, the cron store or the
audit log answers a structured error rather than a 500, and the compensating restore puts
back the prior grant and the pending request but never revives a revoked grant. Non-owner
and machine callers are refused, and neither the owner-view grant map nor the unknown-secret
error echoes a credential-shaped string. The rest of the ``test_cron_secret_env*.py`` tests,
from the validator and the pin to both sandboxed runners and the agent's MCP side, are in
``test_cron_secret_env.py``; the shared fixtures and helpers live in
``cron_secret_env_helpers.py``.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from cron_secret_env_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _GRANTEE_BODY,
    _cron_caller_is_named,
    _grant_script,
    cron_home_fixture,
)

from kiro_crew.cron import CronService
from kiro_crew.cron_script import compute_secret_env_pin, delivery_fingerprint
from kiro_crew.secrets import SecretVault

# The approval body every promote test sends: approval is refused unless it
# restates the request the approver saw — the displayed mapping, the request
# timestamp and the sha256 of the reviewed script source — so the fixture's
# defaults (``_request_pending``'s mapping and ts, the default script digest)
# ride along with ``approve_pending``. Tests whose pending request differs
# override the field(s) that differ.
_APPROVE = {
    "approve_pending": True,
    "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
    "expected_ts": 1.0,
    "expected_source_sha256": hashlib.sha256(_GRANTEE_BODY.encode()).hexdigest(),
}


class TestGrantEndpointPendingFlow:
    """The operator half: approve/deny via the dashboard endpoint."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _owner_view(self, monkeypatch):
        """These tests exercise grant flow, not identity; grant is owner-only."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    def _app_and_svc(self):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())
        app = web.Application()
        app["state"] = SimpleNamespace(crons=svc, push_refresh=lambda *a, **k: None)
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        return app, svc

    def _request_pending(self, svc, cron_home) -> str:
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        pin = compute_secret_env_pin(
            spec,
            "",
            "args",
            job_id=job.id,
            grant={"MY_TOKEN": "slack-sandbox"},
            domain="pending",
            delivery=delivery_fingerprint("dashboard:conftest-slot"),
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=pin,
            secret_env_pending_ts=1.0,
        )
        return job.id

    @pytest.mark.asyncio
    async def test_approve_promotes_pending_to_active(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 200
        # The success echo goes through the owner-view redactor, same as the
        # Schedule listing; an ordinary slug name survives it unchanged.
        assert body["secret_env"] == {"MY_TOKEN": "slack-sandbox"}
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env_pin
        assert job.secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_approval_without_request_snapshot_is_refused(self, cron_home):
        """An approval that omits ``expected_secret_env`` or ``expected_ts``
        attests to nothing about WHICH pending request it blesses — an agent
        could re-issue the request between display and click. Both are
        required, so the omission is refused and nothing is promoted."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        digest_only = {k: v for k, v in _APPROVE.items() if k != "expected_secret_env"}
        no_ts = {k: v for k, v in _APPROVE.items() if k != "expected_ts"}
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                for body in (digest_only, no_ts):
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=body)
                    out = await resp.json()
                    assert resp.status == 400, out
                    assert out["code"] == "invalid_secret_env"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_malformed_stored_grant_is_refused_not_a_500(self, cron_home):
        """The store is agent-writable and loaded verbatim: a non-mapping
        planted into ``secret_env`` must not make the owner's approval of a
        valid pending request raise out of ``dict(...)`` into a 500."""
        import json

        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        records = json.loads(svc._path.read_text(encoding="utf-8"))
        jobs = records["jobs"] if isinstance(records, dict) else records
        for rec in jobs:
            if rec["id"] == jid:
                rec["secret_env"] = ["not", "a", "mapping"]
        svc._path.write_text(json.dumps(records), encoding="utf-8")
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "malformed_grant_state"

    @pytest.mark.asyncio
    async def test_unknown_secret_error_does_not_echo_a_credential_shaped_name(self, cron_home):
        """The vault-name half of a grant is agent-authored and the slug grammar
        admits a bare access-key id, so the "unknown name" error must scrub it
        like every other agent string the dashboard echoes."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j", "args", every_secs=3600, script=spec, session_key="dashboard:conftest-slot"
        )
        shaped = "AKIAIOSFODNN7EXAMPLE"  # credential-shaped, slug-valid, not in the vault
        pin = compute_secret_env_pin(
            spec,
            "",
            "args",
            job_id=job.id,
            grant={"MY_TOKEN": shaped},
            domain="pending",
            delivery=delivery_fingerprint("dashboard:conftest-slot"),
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": shaped},
            secret_env_pending_pin=pin,
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{job.id}/secrets",
                    json={**_APPROVE, "expected_secret_env": {"MY_TOKEN": shaped}},
                )
                text = await resp.text()
        assert resp.status == 400
        assert '"unknown_secret"' in text
        assert shaped not in text

    @pytest.mark.asyncio
    async def test_approve_racing_bump_conflicts_and_kills_pin(self, cron_home):
        """An epoch bump landing between the approval's store swap and its
        commit (a racing revoke or job removal) must refuse the CAS commit,
        return 409, and leave the just-swapped pin DEAD — never re-commit
        the value the concurrent bump minted."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.cron_script import _secret_env_precheck, bump_grant_epoch

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)

        real_update = svc.update_job_async

        async def raced(*args: object, **kwargs: object):
            result = await real_update(*args, **kwargs)
            bump_grant_epoch(jid)  # the concurrent removal/revoke wins the gap
            return result

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch.object(svc, "update_job_async", raced),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "grant_conflict"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env_pin
        resolved, err = _secret_env_precheck(
            job.secret_env,
            job.secret_env_pin,
            script=job.script,
            script_body=(cron_home() / "crons" / "grantee.py").read_bytes(),
            message=job.message,
            job_id=jid,
        )
        assert resolved == {}
        assert err is not None and "code changed" in err

    @pytest.mark.asyncio
    async def test_approve_epoch_commit_failure_keeps_pending(self, cron_home):
        """An unwritable epoch store must not strand the job: the compensating
        write restores BOTH the prior active grant (its pin is still valid —
        the failed commit never advanced the epoch) AND the consumed pending
        request, so the owner re-approves once storage heals — never a dead
        just-swapped pin standing in for the grant that worked before."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        pending_pin_before = CronService(base_dir=svc._dir).get_job(jid).secret_env_pending_pin
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=OSError("no space left on device"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "epoch_commit_failed"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        # The restore carries the original request pin, so the re-approval's
        # code-drift check still runs against what the agent actually asked.
        assert job.secret_env_pending_pin == pending_pin_before
        # FULL compensation: the prior active grant (none here) is restored —
        # the dead just-swapped pin does not survive as the active grant.
        assert job.secret_env == {}
        assert job.secret_env_pin == ""

    @pytest.mark.asyncio
    async def test_epoch_commit_failure_restores_prior_active_grant(self, cron_home):
        """Active grant A + pending B: a failed epoch commit must put A back —
        its pin is still valid (the epoch never advanced), so replacing it
        with B's dead pin would destroy a working grant for nothing."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("other-secret", "xoxb-2")
        jid = self._request_pending(svc, cron_home)
        # Plant prior active grant A directly in the store.
        svc.update_job(jid, secret_env={"OLD_TOKEN": "other-secret"}, secret_env_pin="pin-A")
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=OSError("no space left on device"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 503
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"OLD_TOKEN": "other-secret"}
        assert job.secret_env_pin == "pin-A"
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_consumes_pending_atomically(self, cron_home):
        """The promoting write and the pending-consumption are ONE store
        write: a window between them would let a concurrent deny (or second
        approval) pass its compare-and-swap against the already-promoted
        snapshot and misreport success against the new grant."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        calls: list[dict] = []
        real_update = svc.update_job_async

        async def recording_update(job_id, **kwargs):
            calls.append(dict(kwargs))
            return await real_update(job_id, **kwargs)

        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            with patch.object(svc, "update_job_async", side_effect=recording_update):
                async with TestClient(TestServer(app)) as client:
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 200
        pending_writes = [c for c in calls if "secret_env_pending" in c]
        assert len(pending_writes) == 1
        assert "secret_env" in pending_writes[0]
        assert pending_writes[0]["secret_env_pending"] == {}
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env_pending == {}

    @pytest.mark.asyncio
    async def test_revoke_epoch_bump_failure_is_structured_503(self, cron_home):
        """Corrupt or unwritable epoch state must not crash a revoke into a
        500, and must NOT clear the grant either: without the bump a saved
        copy of the mapping+pin would verify again once the state heals, so
        the handler answers a structured 503 and leaves the store as-is
        (runs already refuse under unhealthy epoch state — fail closed)."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(job.id, secret_env={"MY_TOKEN": "slack-sandbox"}, secret_env_pin="somepin")
        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.bump_grant_epoch",
                side_effect=ValueError("grant-epoch state corrupt (non-integer epoch)"),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{job.id}/secrets", json={"secret_env": {}})
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "epoch_bump_failed"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None
        assert reloaded.secret_env == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_refused_when_audit_unwritable(self, cron_home):
        """AUDIT-OR-DENY: a grant must never exist unaudited. An SEL store
        that cannot take the pre-mutation intent record refuses the approval
        with a structured 503 and NOTHING mutated — pending intact, no
        active grant."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_mock:
            sel_mock.return_value.log_api_access.side_effect = OSError("read-only fs")
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "audit_unavailable"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_decision_audits_are_critical_writes(self, cron_home):
        """The default SEL path enqueues and swallows a filesystem failure, so
        the audit-or-deny refusal above only exists if the intent record is a
        ``critical`` (synchronous, re-raising) write — for approve, deny and
        revoke alike."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_mock:
            log = sel_mock.return_value.log_api_access
            async with TestClient(TestServer(app)) as client:
                await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                jid2 = self._request_pending(svc, cron_home)
                await client.put(f"/api/crons/{jid2}/secrets", json=_APPROVE)
                await client.put(f"/api/crons/{jid2}/secrets", json={"secret_env": {}})
        # Only the pre-mutation INTENT records are gated; the approve path also
        # emits a best-effort terminal "allowed" event, which stays non-critical.
        ops = {
            c.kwargs["operation"]: c.kwargs.get("critical")
            for c in log.call_args_list
            if c.kwargs.get("outcome") == "invoked"
        }
        assert ops["cron.secret_request_denied"] is True
        assert ops["cron.secret_request_approved"] is True
        assert ops["cron.secret_revoke"] is True

    @pytest.mark.asyncio
    async def test_deny_and_revoke_refused_when_audit_unwritable(self, cron_home):
        """Privilege-reducing decisions take the same audit-or-deny gate: an
        unwritable SEL refuses the denial (request stays pending) and the
        revocation (grant stays active), each with 503 audit_unavailable."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        svc.update_job(jid, secret_env={"OLD": "jira-token"}, secret_env_pin="pin")
        with patch("kiro_crew.dashboard.handlers.cron._sel") as sel_mock:
            sel_mock.return_value.log_api_access.side_effect = OSError("read-only fs")
            async with TestClient(TestServer(app)) as client:
                deny = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                deny_body = await deny.json()
                revoke = await client.put(f"/api/crons/{jid}/secrets", json={"secret_env": {}})
                revoke_body = await revoke.json()
        assert (deny.status, deny_body["code"]) == (503, "audit_unavailable")
        assert (revoke.status, revoke_body["code"]) == (503, "audit_unavailable")
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}
        assert job.secret_env == {"OLD": "jira-token"}

    @pytest.mark.asyncio
    async def test_unreadable_store_is_structured_on_approve_and_deny(self, cron_home):
        """An EACCES/EIO store must answer both decisions with the structured
        non-retryable 409 (cron_store_unreadable), never a 500."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.cron import CronStoreUnreadable

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)

        async def unreadable(job_id, **kwargs):
            raise CronStoreUnreadable("crons.json unreadable: EACCES")

        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            with patch.object(svc, "update_job_async", side_effect=unreadable):
                async with TestClient(TestServer(app)) as client:
                    for decision in (_APPROVE, {"deny_pending": True}):
                        resp = await client.put(f"/api/crons/{jid}/secrets", json=decision)
                        body = await resp.json()
                        assert resp.status == 409, (decision, body)
                        assert body["code"] == "cron_store_unreadable"
                        assert body["retryable"] is False

    @pytest.mark.asyncio
    async def test_store_unreadable_on_read_is_409_not_job_not_found(self, cron_home):
        """An unreadable store loads as an EMPTY job list without raising,
        so the job lookup misses. Every decision must answer the unreadable 409
        the other read-decide-write routes answer, never a 404 "job not found"
        over a corrupt file -- and the records on disk stay untouched."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        store = svc._path
        corrupt = store.read_bytes() + b"\xff\xfe"
        store.write_bytes(corrupt)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                for decision in (_APPROVE, {"deny_pending": True}, {"secret_env": {}}):
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=decision)
                    body = await resp.json()
                    assert resp.status == 409, (decision, body)
                    assert body["code"] == "cron_store_unreadable", (decision, body)
                    assert body["retryable"] is False
        assert store.read_bytes() == corrupt

    @pytest.mark.asyncio
    async def test_store_unreadable_at_promotion_read_is_409(self, cron_home):
        """The promotion path re-reads the job on its own. A store that
        turns unreadable between the route's read and that re-read must answer
        the unreadable 409, not a 404 "job not found", and promote nothing."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        store = svc._path
        real_get = svc.get_job_async
        calls = {"n": 0}

        async def corrupt_after_first_read(job_id):
            calls["n"] += 1
            if calls["n"] == 2:
                store.write_bytes(store.read_bytes() + b"\xff\xfe")
            return await real_get(job_id)

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch.object(svc, "get_job_async", side_effect=corrupt_after_first_read),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert calls["n"] == 2, "the promotion path must re-read the job"
        assert resp.status == 409, body
        assert body["code"] == "cron_store_unreadable"
        store.write_bytes(store.read_bytes()[: -len(b"\xff\xfe")])
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    def test_owner_view_grant_map_is_redacted(self):
        """The store is agent-writable and the read path loads grant dicts
        verbatim, so the owner-view serializer must scan keys AND values —
        a mapping planted directly in crons.json with credential- or
        exfil-URL-shaped content must not reach the dashboard raw."""
        from kiro_crew.dashboard.handlers.cron import _redacted_grant_map

        out = _redacted_grant_map({"AWS_KEY": "https://evil.example/exfil?d=AKIAIOSFODNN7EXAMPLE"})
        assert out is not None
        serialized = str(out)
        assert "AKIAIOSFODNN7EXAMPLE" not in serialized
        assert _redacted_grant_map({}) is None
        # A benign product-written mapping passes through unchanged.
        assert _redacted_grant_map({"MY_TOKEN": "slack-sandbox"}) == {"MY_TOKEN": "slack-sandbox"}
        # Malformed agent-planted shapes serialize as None, never a 500.
        assert _redacted_grant_map([]) is None  # type: ignore[arg-type]
        assert _redacted_grant_map("junk") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_epoch_commit_and_compensation_double_fault_is_loud(self, cron_home):
        """When the epoch commit fails AND every compensation attempt fails
        too (store went bad between the writes), the handler must answer a
        structured 503 that says the prior grant was NOT restored — never
        pretend the compensation landed or crash with a 500."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.cron import CronStoreUnreadable

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        real_update = svc.update_job_async
        calls = {"n": 0}

        async def first_write_only(job_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_update(job_id, **kwargs)
            raise CronStoreUnreadable("crons.json unreadable: EIO")

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=OSError("no space left on device"),
            ),
            patch.object(svc, "update_job_async", side_effect=first_write_only),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "epoch_commit_failed"
        assert "restore failed" in body["error"]

    @pytest.mark.asyncio
    async def test_approve_refuses_when_code_changed_after_request(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        # A pending request whose pin was taken over DIFFERENT code than the
        # job now carries — the shape a post-request script rewrite produces.
        spec = _grant_script(cron_home, body="def run(ctx): return 'current'\n")
        other_spec = _grant_script(
            cron_home, body="def run(ctx): return 'requested'\n", name="other.py"
        )
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                other_spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
            ),
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{job.id}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "code_changed"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None and reloaded.secret_env == {}

    @pytest.mark.asyncio
    async def test_deny_with_stale_expected_refuses(self, cron_home):
        """A denial restating request A must not clear a replacement B."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        job_spec = str(cron_home() / "crons" / "grantee.py") + ":run"
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "other-secret"},
            secret_env_pending_pin=compute_secret_env_pin(
                job_spec,
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "other-secret"},
                domain="pending",
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "deny_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "stale_request"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env_pending == {"MY_TOKEN": "other-secret"}

    @pytest.mark.asyncio
    async def test_deny_clears_pending(self, cron_home):
        from aiohttp.test_utils import TestClient, TestServer

        app, svc = self._app_and_svc()
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json={"deny_pending": True})
        assert resp.status == 200
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        assert job.secret_env_pending == {}
        assert job.secret_env == {}

    @pytest.mark.asyncio
    async def test_compensation_never_resurrects_concurrent_revoke(self, cron_home):
        """Epoch commit fails AND a concurrent revoke cleared the grant in
        the gap: the compensating restore must yield to the revocation — the
        active-field compare-and-swap sees the fields do not hold the
        just-promoted grant and skips, never writing grant A back over what
        the operator withdrew."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("other-secret", "xoxb-2")
        jid = self._request_pending(svc, cron_home)
        svc.update_job(jid, secret_env={"OLD_TOKEN": "other-secret"}, secret_env_pin="pin-A")

        def failing_commit(job_id, next_epoch, expected_current):
            # Simulate the concurrent revoke landing in the exact gap: the
            # store clears the active fields BEFORE the handler's
            # compensation runs, then the commit itself fails.
            svc.update_job(jid, secret_env={}, secret_env_pin="")
            raise OSError("no space left on device")

        with (
            patch("kiro_crew.dashboard.handlers.cron._sel"),
            patch(
                "kiro_crew.dashboard.handlers.cron.commit_grant_epoch",
                side_effect=failing_commit,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 503
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None
        # The revocation stands: neither grant A nor the dead promoted grant
        # may be resurrected by the compensation.
        assert job.secret_env == {}
        assert job.secret_env_pin == ""

    @pytest.mark.asyncio
    async def test_approve_without_source_digest_is_refused(self, cron_home):
        """An approval that does not restate the reviewed source digest is a
        400: it attests to nothing about the code, so it cannot promote."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                for bad in (
                    {"approve_pending": True},
                    {"approve_pending": True, "expected_source_sha256": ""},
                    {"approve_pending": True, "expected_source_sha256": "ABC"},
                    {"approve_pending": True, "expected_source_sha256": 42},
                ):
                    resp = await client.put(f"/api/crons/{jid}/secrets", json=bad)
                    body = await resp.json()
                    assert resp.status == 400, (bad, body)
                    assert body["code"] == "invalid_secret_env"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}
        assert job.secret_env_pending == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_of_reissued_request_over_unseen_source_is_refused(self, cron_home):
        """The reissue TOCTOU: the operator loaded source A, the agent rewrote
        the script to B and re-issued the request (pin now over B), and the
        dashboard's request banner refreshed while its source view did not.
        The approval restates B's mapping and timestamp — both request-level
        checks pass and the pin verifies against B — but its source digest is
        A's, so the promotion must refuse rather than bless code nobody read."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        jid = self._request_pending(svc, cron_home)
        seen_sha = _APPROVE["expected_source_sha256"]  # digest of revision A
        # Agent rewrites the script (revision B) and re-issues the request.
        spec = _grant_script(cron_home, body="def run(ctx): return 'B'\n")
        job = svc.get_job(jid)
        assert job is not None
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
                delivery=delivery_fingerprint("dashboard:conftest-slot"),
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "approve_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                        "expected_ts": 2.0,
                        "expected_source_sha256": seen_sha,
                    },
                )
                body = await resp.json()
                assert resp.status == 409, body
                assert body["code"] == "stale_source"
                # Restating B's digest — what a re-loaded source view shows —
                # is the one approval that promotes.
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        "approve_pending": True,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                        "expected_ts": 2.0,
                        "expected_source_sha256": hashlib.sha256(
                            b"def run(ctx): return 'B'\n"
                        ).hexdigest(),
                    },
                )
                assert resp.status == 200, await resp.json()
        reloaded = CronService(base_dir=svc._dir).get_job(jid)
        assert reloaded is not None and reloaded.secret_env == {"MY_TOKEN": "slack-sandbox"}

    @pytest.mark.asyncio
    async def test_approve_refuses_source_the_display_cannot_show_verbatim(self, cron_home):
        """A body whose dashboard rendering differs from its raw bytes (the
        redactor masked a credential-shaped span here; undecodable bytes are
        the other case) is not approvable even with the CORRECT raw digest:
        the operator could never have read the masked span, so an agent could
        hide executable content behind it."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc = self._app_and_svc()
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        body = 'KEY = "AKIAIOSFODNN7EXAMPLE"\ndef run(ctx): pass\n'
        spec = _grant_script(cron_home, body=body)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:conftest-slot",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
                delivery=delivery_fingerprint("dashboard:conftest-slot"),
            ),
            secret_env_pending_ts=1.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{job.id}/secrets",
                    json={
                        **_APPROVE,
                        "expected_source_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    },
                )
                out = await resp.json()
        assert resp.status == 409, out
        assert out["code"] == "source_not_reviewable"
        reloaded = CronService(base_dir=svc._dir).get_job(job.id)
        assert reloaded is not None and reloaded.secret_env == {}


class TestGrantEndpointBoundaries:
    """Identity boundaries of the owner-only grant endpoint."""

    def _apps(self, *, internal_auth: bool):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.config.loader import config_dir as loader_config_dir
        from kiro_crew.dashboard.handlers.cron import api_cron_secret_grant

        svc = CronService(base_dir=loader_config_dir())

        @web.middleware
        async def _mark_internal(request, handler):
            if internal_auth:
                request["internal_auth"] = True
            return await handler(request)

        state = SimpleNamespace(
            crons=svc,
            push_refresh=lambda *a, **k: None,
            _slots={"chat-1-abc": object()},
        )
        app = web.Application(middlewares=[_mark_internal])
        app["state"] = state
        app.router.add_put("/api/crons/{job_id}/secrets", api_cron_secret_grant)
        return app, svc, state

    def _pending_job(self, svc, cron_home) -> str:
        spec = _grant_script(cron_home)
        job = svc.add_job(
            "j",
            "args",
            every_secs=3600,
            script=spec,
            session_key="dashboard:chat-1-abc",
        )
        svc.update_job(
            job.id,
            secret_env_pending={"MY_TOKEN": "slack-sandbox"},
            secret_env_pending_pin=compute_secret_env_pin(
                spec,
                "",
                "args",
                job_id=job.id,
                grant={"MY_TOKEN": "slack-sandbox"},
                domain="pending",
            ),
            secret_env_pending_ts=1.0,
        )
        return job.id

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _owner_view(self, monkeypatch):
        """Default to owner view; the non-owner test overrides it."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

    @pytest.mark.asyncio
    async def test_direct_grant_is_refused(self, cron_home):
        """Direct grants are removed: nothing binds what the owner saw to
        what gets pinned, so the request->approve flow is the only mint
        path. The endpoint keeps revoke and pending decisions only."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc, _state = self._apps(internal_auth=False)
        jid = self._pending_job(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={"secret_env": {"MY_TOKEN": "slack-sandbox"}},
                )
                body = await resp.json()
        assert resp.status == 400
        assert body["code"] == "direct_grant_removed"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_replaced_pending_request_is_not_promoted_by_stale_approval(self, cron_home):
        """The agent can overwrite a pending request at any moment; an
        approval that restates request A must not promote a replacement B."""
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.config.loader import config_dir as loader_config_dir

        app, svc, _state = self._apps(internal_auth=False)
        SecretVault(loader_config_dir()).set_sync("slack-sandbox", "xoxb-1")
        SecretVault(loader_config_dir()).set_sync("jira-token", "fake-2")
        jid = self._pending_job(svc, cron_home)  # pending: MY_TOKEN <- slack-sandbox
        # Agent replaces the request AFTER the operator's view rendered.
        svc.update_job(
            jid,
            secret_env_pending={"MY_TOKEN": "jira-token"},
            secret_env_pending_pin=compute_secret_env_pin(
                str(cron_home() / "crons" / "grantee.py") + ":run",
                "",
                "args",
                job_id=jid,
                grant={"MY_TOKEN": "jira-token"},
                domain="pending",
            ),
            secret_env_pending_ts=2.0,
        )
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    f"/api/crons/{jid}/secrets",
                    json={
                        **_APPROVE,
                        "expected_secret_env": {"MY_TOKEN": "slack-sandbox"},
                    },
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "stale_request"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_token_cannot_grant(self, monkeypatch, cron_home):
        """A dashboard token minted for an allowed Slack user (not the owner)
        must not approve grants — the vault boundary is owner-only."""
        from aiohttp.test_utils import TestClient, TestServer

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        app, svc, _state = self._apps(internal_auth=False)
        jid = self._pending_job(svc, cron_home)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
        assert resp.status == 403
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}

    @pytest.mark.asyncio
    async def test_grant_endpoint_refuses_machine_credential(self, cron_home):
        """/api/crons is a prefix internal path, so the grant route IS
        reachable with X-Internal-Secret — the handler must refuse it."""
        from aiohttp.test_utils import TestClient, TestServer

        app, svc, _state = self._apps(internal_auth=True)
        jid = self._pending_job(svc, cron_home)
        with patch("kiro_crew.dashboard.handlers.cron._sel"):
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(f"/api/crons/{jid}/secrets", json=_APPROVE)
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "operator_only"
        job = CronService(base_dir=svc._dir).get_job(jid)
        assert job is not None and job.secret_env == {}
