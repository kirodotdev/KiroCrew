"""Coverage for ``dashboard/handlers/core.py``: security reads and the local-secret gates.

The SEL and security-posture read endpoints, the loopback-gated local endpoints
(token / logout), the app-secret exchange, and a non-ASCII ``X-Local-Secret`` at each
local-secret gate.

Style and isolation follow ``test_dashboard_handlers_core_coverage.py``: direct
handler calls against a ``MagicMock(spec=web.Request)``, a real aiohttp ``TestClient``
where the handler needs a genuine request body or streaming response, and every write
under the autouse-isolated ``KIROCREW_HOME`` from ``conftest.py``; nothing here touches
the network, downloads a model, or spawns a process. Shared stubs and fixtures come
from ``dashboard_handlers_core_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _req

from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.sel import SelVerification as _SelVerification

# ── Security event log + posture ────────────────────────────────────────


class TestSelEndpoints:
    #: The audit trail is owner-only, so every read below is made AS the owner.
    _OWNER_APP = {"state": SimpleNamespace(owner_id="dashboard")}

    def _owner_req(self, **kwargs):
        return _req(app=self._OWNER_APP, user="dashboard", **kwargs)

    @pytest.mark.asyncio
    async def test_events_uses_default_limit(self, fake_sel) -> None:
        fake_sel.recent.return_value = [{"event": "a"}]
        resp = await core_mod.api_sel_events(self._owner_req())
        assert json.loads(resp.body) == {"events": [{"event": "a"}], "count": 1}
        assert fake_sel.recent.call_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_events_caps_limit_at_1000(self, fake_sel) -> None:
        fake_sel.recent.return_value = []
        await core_mod.api_sel_events(self._owner_req(query={"limit": "99999"}))
        assert fake_sel.recent.call_args.kwargs["limit"] == 1000

    @pytest.mark.asyncio
    async def test_events_falls_back_on_unparsable_limit(self, fake_sel) -> None:
        fake_sel.recent.return_value = []
        await core_mod.api_sel_events(self._owner_req(query={"limit": "many"}))
        assert fake_sel.recent.call_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_a_successful_owner_read_is_audited_after_it_is_served(self, fake_sel) -> None:
        """A trail of refusals alone never says the log was read.

        The denial branch records who was turned away; without a matching row for
        the read that succeeded, an operator reviewing the trail cannot tell an
        untouched log from one the owner has been reading. The write lands AFTER the
        rows are captured, because ``recent()`` flushes the write queue before it
        walks the log: enqueued first, this row would reach disk in time to be
        served back as the newest event, and a ``limit=1`` read would return nothing
        but its own audit.
        """
        calls: list[str] = []
        fake_sel.log_api_access.side_effect = lambda **kw: calls.append(f"audit:{kw['outcome']}")
        fake_sel.recent.side_effect = lambda **kw: calls.append("read") or [{"event": "a"}]

        resp = await core_mod.api_sel_events(self._owner_req())

        assert resp.status == 200
        assert calls == ["read", "audit:allowed"]
        assert json.loads(resp.body) == {"events": [{"event": "a"}], "count": 1}
        kwargs = fake_sel.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "sel.events.read"
        assert kwargs["outcome"] == "allowed"
        assert kwargs["caller"] == "dashboard"

    @pytest.mark.asyncio
    async def test_a_refused_read_writes_no_allow_row(self, fake_sel) -> None:
        """The negative: the allow row must mean authorized, not merely attempted.

        A refused caller never reaches this handler's own audit, so it writes
        nothing at all here -- the denial row belongs to the shared owner gate and
        is covered where that gate lives. What this pins is that the allow row
        cannot be produced by an attempt.
        """
        resp = await core_mod.api_sel_events(_req(app=self._OWNER_APP, user="someone-else"))

        assert resp.status == 403
        fake_sel.log_api_access.assert_not_called()
        fake_sel.recent.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"user": "someone-else"},
            {"user": None},
            {"app_token": "an-app"},
            {"app_token": None},
        ],
        ids=["other-user", "no-user", "app-token", "absent-app-claim"],
    )
    async def test_events_refuses_every_non_owner_caller(self, fake_sel, kwargs) -> None:
        """The rows name the resources a security decision was about.

        A dashboard session is not by itself the owner: the messaging bridges
        mint a presigned token whose subject is the allowed user's own id, so
        serving these rows to any authenticated session hands one principal the
        other's audit trail. Each parameter is a caller class that must fail
        closed, and the read must not happen AT ALL -- a 403 whose body still
        carried the rows would pass a status-only assertion.
        """
        resp = await core_mod.api_sel_events(_req(app=self._OWNER_APP, **kwargs))
        assert resp.status == 403
        assert json.loads(resp.body) == {
            "error": "owner authorization required",
            "code": "owner_only",
        }
        fake_sel.recent.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("subject", ["local-app", "local-startup"])
    async def test_events_tells_a_pre_owner_session_to_sign_in_again(
        self, fake_sel, subject
    ) -> None:
        """A bootstrap subject under a configured owner IS the owner, refused.

        Configuring an owner after the dashboard session was signed leaves that
        session's subject at the bootstrap name, and a token refresh preserves the
        subject, so the real owner keeps failing the gate until they sign in again.
        A generic 403 gives the one caller class that can act on the refusal no way
        to know that, which is why the denial goes through the shared tail: the
        status is 401 and the code names re-authentication. The read must still not
        happen -- the relabel changes the response, not the decision.
        """
        resp = await core_mod.api_sel_events(_req(app=self._OWNER_APP, user=subject, app_token=""))
        assert resp.status == 401
        assert json.loads(resp.body)["code"] == "stale_session_reauth"
        fake_sel.recent.assert_not_called()

    @pytest.mark.asyncio
    async def test_events_keeps_a_generic_403_when_no_owner_is_configured(self, fake_sel) -> None:
        """No owner configured means a bootstrap subject is not stale.

        ``is_owner_dashboard_request`` admits the implicit local owner in that
        configuration, so this asserts the gate does not hand out the 401 to a
        caller whose credential is current: reaching the relabel requires a
        CONFIGURED owner, and the subject here is simply not that owner.
        """
        resp = await core_mod.api_sel_events(
            _req(app={"state": SimpleNamespace(owner_id="")}, user="someone-else")
        )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "owner_only"
        fake_sel.recent.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_reports_intact_chain(self, fake_sel) -> None:
        fake_sel.verify_integrity.return_value = _SelVerification(7, 7, True, "")
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body == {
            "total": 7,
            "valid": 7,
            "integrity": "ok",
            "tampered": 0,
            "detail": "",
            "dropped_events": 0,
        }

    @pytest.mark.asyncio
    async def test_verify_reports_tampering(self, fake_sel) -> None:
        fake_sel.verify_integrity.return_value = _SelVerification(7, 5, True, "")
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body["integrity"] == "compromised"
        assert body["tampered"] == 2

    @pytest.mark.asyncio
    async def test_verify_reports_unverifiable_history(self, fake_sel) -> None:
        """A refused pin must not answer ``ok`` over the live log alone."""
        fake_sel.verify_integrity.return_value = _SelVerification(
            7, 7, False, "segment directory refused to pin (planted link?)"
        )
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body["integrity"] == "unverifiable"
        assert "refused" in body["detail"]

    @pytest.mark.asyncio
    async def test_verify_reports_dropped_events(self, fake_sel) -> None:
        """The writer's lost-batch counter is surfaced next to the chain verdict."""
        fake_sel.verify_integrity.return_value = _SelVerification(7, 7, True, "")
        fake_sel.dropped_events = 4
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body["dropped_events"] == 4


class TestSecurityStats:
    @pytest.mark.asyncio
    async def test_counts_are_derived_from_the_posture_registry(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.security.build_denied_commands_snapshot_async",
            AsyncMock(return_value={"effective_count": 42}),
        )
        monkeypatch.setattr(
            core_mod,
            "posture_counts_async",
            AsyncMock(
                return_value={
                    "suspicious_patterns": 3,
                    "tool_schemas": 4,
                    "redaction_paths": 5,
                }
            ),
        )
        body = json.loads((await core_mod.api_security_stats(_req())).body)
        assert body == {
            "denied_commands": 42,
            "suspicious_patterns": 3,
            "tool_schemas": 4,
            "redaction_paths": 5,
        }

    @pytest.mark.asyncio
    async def test_denied_count_failure_degrades_to_zero(self, monkeypatch) -> None:
        """An unreadable denylist must not take the whole stats endpoint down."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.security.build_denied_commands_snapshot_async",
            AsyncMock(side_effect=OSError("unreadable")),
        )
        monkeypatch.setattr(core_mod, "posture_counts_async", AsyncMock(return_value={}))
        body = json.loads((await core_mod.api_security_stats(_req())).body)
        assert body["denied_commands"] == 0
        assert body["suspicious_patterns"] is None


# ── Loopback + local-secret gated endpoints ─────────────────────────────


class TestLocalToken:
    @pytest.fixture
    def verified_owner_process(self, monkeypatch) -> None:
        from kiro_crew import member_memory_auth as auth

        peer_pid = 12345
        monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: peer_pid)
        monkeypatch.setattr(
            auth.platform_compat,
            "get_process_start_id",
            lambda pid: "synthetic-start" if pid == peer_pid else None,
        )
        monkeypatch.setattr(auth, "_verified_host_process", lambda pid: pid == peer_pid)

    @pytest.mark.asyncio
    async def test_non_loopback_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        resp = await core_mod.api_token_local(_req(remote="203.0.113.9"))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "non-loopback"

    @pytest.mark.asyncio
    async def test_unconfigured_secret_is_503(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(_req(app={}))
        assert resp.status == 503
        assert json.loads(resp.body)["error"] == "not available"

    @pytest.mark.asyncio
    async def test_wrong_secret_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_missing_secret_header_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(_req(app={"local_secret": "right"}))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_issues_credential_with_requested_ttl_and_embed_claim(
        self, monkeypatch, fake_sel, verified_owner_process
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        minted: dict = {}

        def _generate(owner, ttl_seconds=0, extra=None):
            minted.update({"owner": owner, "ttl": ttl_seconds, "extra": extra})
            return "issued-value"

        monkeypatch.setattr(core_mod, "generate_token", _generate)
        resp = await core_mod.api_token_local(
            _req(
                app={"local_secret": "right", "state": SimpleNamespace(owner_id="owner-1")},
                headers={"X-Local-Secret": "right"},
                query={"ttl": "2h", "embed_parent_port": "5476"},
            )
        )
        assert resp.status == 200
        assert json.loads(resp.body)["expires_in"] == 7200
        assert minted["owner"] == "owner-1"
        assert minted["extra"] == {"embed_parent_port": "5476"}

    @pytest.mark.asyncio
    async def test_a_host_process_mint_is_audited_without_an_app(
        self, monkeypatch, fake_sel, verified_owner_process
    ) -> None:
        """An ordinary host process is recorded with no app name."""
        from kiro_crew import member_memory_auth as auth

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        monkeypatch.setattr(auth, "_gateway_spawned_app_backend", lambda _pid: None)
        monkeypatch.setattr(core_mod, "generate_token", lambda *a, **k: "issued-value")
        resp = await core_mod.api_token_local(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 200
        assert set(json.loads(resp.body)) == {"token", "expires_in"}
        row = fake_sel.log_api_access.call_args.kwargs
        assert row["operation"] == "token.local"
        assert row["outcome"] == "success"
        assert row["resources"] == "token-issued"

    @pytest.mark.asyncio
    async def test_an_app_backend_mint_is_attributed_to_the_app(
        self, monkeypatch, fake_sel
    ) -> None:
        """The owning app's name reaches the audit row, not the response or the token.

        The registry knows which app's backend owns the calling pid; the gate
        hands that name through so forensics can tell which app minted an owner
        token. The token itself carries no ``app`` claim: an app-scoped token is
        refused by the owner-dashboard check, which would withdraw the capability
        the admission grants.
        """
        from kiro_crew import member_memory_auth as auth

        peer_pid = 4242
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: peer_pid)
        monkeypatch.setattr(
            auth.platform_compat,
            "get_process_start_id",
            lambda pid: "synthetic-start" if pid == peer_pid else None,
        )
        monkeypatch.setattr(auth, "_verified_host_process", lambda _pid: False)
        monkeypatch.setattr(
            auth,
            "_gateway_spawned_app_backend",
            lambda pid: "demo-app" if pid == peer_pid else None,
        )
        minted: dict = {}

        def _generate(owner, ttl_seconds=0, extra=None):
            minted.update({"owner": owner, "extra": extra})
            return "issued-value"

        monkeypatch.setattr(core_mod, "generate_token", _generate)
        resp = await core_mod.api_token_local(
            _req(
                app={"local_secret": "right", "state": SimpleNamespace(owner_id="owner-1")},
                headers={"X-Local-Secret": "right"},
            )
        )
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body == {"token": "issued-value", "expires_in": core_mod.MAX_SESSION_TTL_SECS}
        assert minted["owner"] == "owner-1"
        assert minted["extra"] is None
        row = fake_sel.log_api_access.call_args.kwargs
        assert row["operation"] == "token.local"
        assert row["outcome"] == "success"
        assert row["resources"] == "token-issued app=demo-app"

    @pytest.mark.asyncio
    async def test_unix_peer_match_with_valid_secret_issues_a_token(
        self, monkeypatch, fake_sel, verified_owner_process
    ) -> None:
        """A kernel-verified same-uid AF_UNIX peer is admitted.

        The pod's `mint_token` connects over the pod's private unix socket,
        where ``request.remote`` is EMPTY -- the loopback test alone 403s the
        transport that is strictly harder to reach than loopback TCP. The
        positive `check_peer_is_self` MATCH plus the unchanged secret check
        must mint.
        """
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MATCH,
        )
        monkeypatch.setattr(core_mod, "generate_token", lambda *a, **k: "issued-value")
        resp = await core_mod.api_token_local(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 200
        assert json.loads(resp.body)["token"] == "issued-value"

    @pytest.mark.asyncio
    async def test_unix_peer_still_needs_the_secret(self, monkeypatch, fake_sel) -> None:
        """Unix admission is a TRANSPORT gate: the secret check is unchanged.

        A MATCH peer with the wrong secret is refused at the SECRET check
        ("invalid secret"), not the transport check ("loopback only") -- which
        also pins that the admitted request reached past the transport gate.
        """
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MATCH,
        )
        resp = await core_mod.api_token_local(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_unix_peer_uid_mismatch_is_refused_even_with_the_secret(
        self, monkeypatch, fake_sel
    ) -> None:
        """A foreign-uid unix peer is refused BEFORE the secret is considered.

        MISMATCH means the kernel positively identified another principal on
        our socket -- exactly when the 0700-home directory gate has failed and
        denying matters most. Deny-by-default: transport refusal even though
        the request carries the correct secret.
        """
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MISMATCH,
        )
        resp = await core_mod.api_token_local(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "non-loopback"

    @pytest.mark.asyncio
    async def test_unix_peer_unverifiable_is_refused_even_with_the_secret(
        self, monkeypatch, fake_sel
    ) -> None:
        """Failure to verify the peer is never conflated with permission.

        UNVERIFIABLE (no mechanism, non-AF_UNIX family, syscall failure) must
        refuse -- a platform without a peer-credential mechanism never silently
        widens this token_auth-bypassed endpoint.
        """
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.UNVERIFIABLE,
        )
        resp = await core_mod.api_token_local(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"

    @pytest.mark.asyncio
    async def test_valid_secret_without_verified_owner_process_is_refused(
        self, monkeypatch, fake_sel
    ) -> None:
        from kiro_crew import member_memory_auth as auth

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: None)
        minted = MagicMock()
        monkeypatch.setattr(core_mod, "generate_token", minted)
        resp = await core_mod.api_token_local(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "member_owner_token_refused"
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "unverified-owner-process"
        minted.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shares_namespaces,status", [(False, 403), (True, 200)])
    async def test_linux_namespace_divergence_alone_decides_the_owner_verdict(
        self, monkeypatch, fake_sel, shares_namespaces, status
    ) -> None:
        """On Linux the namespace comparison is the whole owner verdict.

        The other refusal tests on this route hand the gate no peer pid, which
        answers on its first leg and leaves the Linux measure unreached from here.
        In this one the kernel DOES identify the caller and its start id IS
        readable, so the two callers below differ by one bit: whether they share
        the gateway's user and mount namespaces. A same-uid caller that does not
        is refused, and the refusal carries the code a mint reads its remedy from
        beside the audit value the pod records for it.

        Both directions are asserted, because a gate that refused every caller
        would satisfy the negative one on its own. The comparison is also pinned
        to the gateway's own pid, which is what makes it a statement about this
        process rather than about any two processes. The app-backend escape is
        held off so one cause decides the verdict; it is covered separately.
        """
        from kiro_crew import member_memory_auth as auth

        peer_pid = 12345
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        monkeypatch.setattr(auth, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: peer_pid)
        monkeypatch.setattr(auth, "_gateway_spawned_app_backend", lambda _pid: None)
        monkeypatch.setattr(
            auth.platform_compat, "get_process_start_id", lambda _pid: "synthetic-start"
        )
        namespaces = MagicMock(return_value=shares_namespaces)
        monkeypatch.setattr(auth.platform_compat, "process_namespaces_match", namespaces)
        minted = MagicMock(return_value="issued-value")
        monkeypatch.setattr(core_mod, "generate_token", minted)

        resp = await core_mod.api_token_local(
            _req(
                app={"local_secret": "right", "state": SimpleNamespace(owner_id="owner-1")},
                headers={"X-Local-Secret": "right"},
            )
        )

        assert resp.status == status
        namespaces.assert_called_once_with(peer_pid, os.getpid())
        if status == 403:
            assert json.loads(resp.body)["code"] == "member_owner_token_refused"
            assert (
                fake_sel.log_api_access.call_args.kwargs["resources"] == "unverified-owner-process"
            )
            minted.assert_not_called()
        else:
            minted.assert_called_once()

    @pytest.mark.asyncio
    async def test_every_refusal_carries_a_code_matching_its_audit_record(
        self, monkeypatch, fake_sel
    ) -> None:
        """All three gates are machine-distinguishable to the caller, not just one.

        The SEL record already tells the three apart. A caller that can read only
        one of them has to guess between the other two, and their remedies differ:
        one is a worktree update, one regenerates the secret, and the third is about
        which process called and is fixed by neither. Each ``code`` therefore pairs
        with the ``resources`` value written for the same refusal, so the pairing
        cannot drift apart silently.
        """
        from kiro_crew import member_memory_auth as auth

        seen: dict[str, str] = {}

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        resp = await core_mod.api_token_local(_req(remote="203.0.113.9"))
        assert resp.status == 403
        seen[json.loads(resp.body)["code"]] = fake_sel.log_api_access.call_args.kwargs["resources"]

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
        )
        assert resp.status == 403
        seen[json.loads(resp.body)["code"]] = fake_sel.log_api_access.call_args.kwargs["resources"]

        monkeypatch.setattr(auth, "_request_peer_pid", lambda _request: None)
        resp = await core_mod.api_token_local(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 403
        seen[json.loads(resp.body)["code"]] = fake_sel.log_api_access.call_args.kwargs["resources"]

        assert seen == {
            "loopback_only": "non-loopback",
            "invalid_secret": "invalid-secret",
            "member_owner_token_refused": "unverified-owner-process",
        }

    @pytest.mark.asyncio
    async def test_the_codes_do_not_disclose_more_than_the_error_text(
        self, monkeypatch, fake_sel
    ) -> None:
        """A code restates the refusal the body already names in words.

        The endpoint is reachable without a credential, so anything added to its
        refusal body is readable by whoever could already read the ``error``
        string. These two codes carry no fact that string does not.
        """
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        body = json.loads((await core_mod.api_token_local(_req(remote="203.0.113.9"))).body)
        assert body["error"] == "loopback only"
        assert body["code"] == "loopback_only"

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        body = json.loads(
            (
                await core_mod.api_token_local(
                    _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
                )
            ).body
        )
        assert body["error"] == "invalid secret"
        assert body["code"] == "invalid_secret"

    @pytest.mark.asyncio
    async def test_bad_embed_port_is_dropped(
        self, monkeypatch, fake_sel, verified_owner_process
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        minted: dict = {}

        def _generate(owner, ttl_seconds=0, extra=None):
            minted["extra"] = extra
            return "issued-value"

        monkeypatch.setattr(core_mod, "generate_token", _generate)
        resp = await core_mod.api_token_local(
            _req(
                app={"local_secret": "right"},
                headers={"X-Local-Secret": "right"},
                query={"ttl": "not-a-duration", "embed_parent_port": "99999"},
            )
        )
        assert resp.status == 200
        assert minted["extra"] is None


class TestLogout:
    @pytest.mark.asyncio
    async def test_non_loopback_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        resp = await core_mod.api_logout(_req(remote="203.0.113.9"))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"

    @pytest.mark.asyncio
    async def test_wrong_secret_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_logout(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_unix_peer_match_is_admitted_and_revokes(self, monkeypatch, fake_sel) -> None:
        """A kernel-verified same-principal AF_UNIX peer is admitted.

        `kirocrew logout` reaches the gateway over the owner-only unix socket,
        where ``request.remote`` is EMPTY -- the loopback test alone would 403
        the transport. The positive `check_peer_is_self` MATCH (plus the
        unchanged secret) must revoke, like /api/token/local.
        """
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MATCH,
        )
        revoke = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.revoke_all_sessions", revoke)
        resp = await core_mod.api_logout(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}
        revoke.assert_called_once()

    @pytest.mark.asyncio
    async def test_unix_peer_mismatch_is_refused(self, monkeypatch, fake_sel) -> None:
        """A foreign-principal AF_UNIX peer is refused before the secret, so the
        unix admission cannot widen the gate beyond a same-user caller."""
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MISMATCH,
        )
        resp = await core_mod.api_logout(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"

    @pytest.mark.asyncio
    async def test_revocation_persist_failure_reports_a_coded_error(
        self, monkeypatch, fake_sel
    ) -> None:
        """Fail-closed: an unpersisted revocation must never report success."""
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)

        def _boom():
            raise OSError("read-only trust dir")

        monkeypatch.setattr("kiro_crew.dashboard.token_auth.revoke_all_sessions", _boom)
        resp = await core_mod.api_logout(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 500
        body = json.loads(resp.body)
        assert body["code"] == "revocation_persist_failed"
        assert "logout not completed" in body["error"]

    @pytest.mark.asyncio
    async def test_successful_revocation_is_audited(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        revoke = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.revoke_all_sessions", revoke)
        resp = await core_mod.api_logout(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}
        revoke.assert_called_once()
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "success"


class TestShutdownUnixPeer:
    """``POST /api/shutdown`` admits a kernel-attested same-user AF_UNIX peer.

    ``kirocrew stop``'s authenticated fallback sends over the owner-only
    socket when it exists, where ``request.remote`` is EMPTY; the
    loopback test alone would 403 it. The secret check is unchanged.
    """

    @pytest.mark.asyncio
    async def test_unix_peer_match_is_admitted(self, monkeypatch, fake_sel) -> None:
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MATCH,
        )
        event = MagicMock()
        monkeypatch.setattr("kiro_crew.shutdown_event", event)
        resp = await core_mod.api_shutdown(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True, "shutting_down": True}

    @pytest.mark.asyncio
    async def test_unix_peer_mismatch_is_refused(self, monkeypatch, fake_sel) -> None:
        from kiro_crew.mcp_gateway.socketsec import PeerCredResult

        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.request_is_unix_socket", lambda _r: True
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.core.check_peer_is_self",
            lambda _s: PeerCredResult.MISMATCH,
        )
        resp = await core_mod.api_shutdown(
            _req(remote="", app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"


class TestAppSecretExchange:
    @pytest.fixture
    def app_sel(self, monkeypatch) -> MagicMock:
        recorder = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
        return recorder

    @pytest.mark.asyncio
    async def test_missing_header_is_refused(self, app_sel) -> None:
        resp = await core_mod.api_app_token(_req(match_info={"name": "meetings"}))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "missing X-App-Secret header"
        assert app_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_invalid_secret_is_refused(self, app_sel, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: False)
        resp = await core_mod.api_app_token(
            _req(match_info={"name": "meetings"}, headers={"X-App-Secret": "nope"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_valid_secret_mints_an_app_scoped_credential(self, app_sel, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: True)
        seen: dict = {}

        def _generate(name, app=None):
            seen.update({"name": name, "app": app})
            return "app-scoped-value"

        monkeypatch.setattr("kiro_crew.dashboard.token_auth.generate_token", _generate)
        resp = await core_mod.api_app_token(
            _req(match_info={"name": "meetings"}, headers={"X-App-Secret": "ok"})
        )
        assert resp.status == 200
        # The app identity must be IN the payload so downstream middleware can
        # extract a verified app name rather than trusting a header.
        assert seen == {"name": "meetings", "app": "meetings"}
        assert app_sel.log_api_access.call_args.kwargs["outcome"] == "granted"


class TestNonAsciiLocalSecret:
    """A non-ASCII ``X-Local-Secret`` is a wrong secret, never a crash.

    ``hmac.compare_digest`` raises TypeError on a str holding a non-ASCII
    character, so each of the three local-secret gates answered 500 and never
    wrote its denial record. A raw non-UTF-8 header byte decodes to a lone
    surrogate, which is the second parameter.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler", ["api_token_local", "api_logout", "api_shutdown"])
    @pytest.mark.parametrize("bad", ["é", "\udcff", "\ud800"])
    async def test_refused_like_a_wrong_secret(
        self, monkeypatch, fake_sel, handler: str, bad: str
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await getattr(core_mod, handler)(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": bad})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "invalid-secret"
