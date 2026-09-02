"""Tests for RFC notification-bus Phase 5: TTL sweeper and the
send_notification agent tool + endpoint."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import api_notification_agent_push
from kiro_crew.dashboard.state import DashboardState, sweep_expired_notifications


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _note(**over):
    base = {
        "kind": "cron",
        "source": "system",
        "channel": "system.cron",
        "priority": "default",
        "title": "t",
        "body": "b",
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }
    base.update(over)
    return base


class TestTtlSweeper:
    def test_huge_ttl_does_not_abort_sweep(self):
        # GPT 5.6 round 11 (MEDIUM): epoch + huge-int ttl raised
        # OverflowError, and the sweep-wide guard aborted the WHOLE sweep --
        # one poison row kept every expired notification alive.
        expired = _note(priority="passive", ttl=60, ts=str(time.time() - 120))
        huge = _note(priority="passive", ttl=10**300, ts=str(time.time() - 120))
        log = [expired, huge]
        removed = sweep_expired_notifications(log)
        assert removed == 1  # expired row swept despite the huge-ttl row
        assert log == [huge]  # huge ttl is simply not expired -- kept

    def test_trim_sweeps_expired_before_cap(self, tmp_path):
        # GPT 5.6 round 12 (HIGH): append-time trim had the same
        # displacement hazard as the load path -- raw tail-trim retained
        # newer expired-passive rows while deleting older LIVE rows.
        import json as _json

        from kiro_crew.dashboard.state import (
            _MAX_PERSISTED_NOTIFICATIONS,
            _maybe_trim_notifications,
        )

        path = tmp_path / "notifications.jsonl"
        live = _MAX_PERSISTED_NOTIFICATIONS
        rows = [_json.dumps(_note(title=f"live-{i}")) for i in range(live)]
        rows += [
            _json.dumps(
                _note(priority="passive", ttl=60, ts=str(time.time() - 120))
            )
            for _ in range(live + 1)  # push past the 2x trim threshold
        ]
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        _maybe_trim_notifications(path)
        kept = [_json.loads(line) for line in path.read_text().splitlines()]
        titles = {n.get("title") for n in kept}
        assert "live-0" in titles and f"live-{live - 1}" in titles
        assert all(n.get("priority") != "passive" for n in kept)

    def test_load_sweeps_before_truncation(self, monkeypatch, tmp_path):
        # GPT 5.6 round 11 (HIGH): truncating to the cap BEFORE sweeping let
        # newer expired-passive rows displace older LIVE rows; the sweep then
        # removed the expired rows and the next rewrite deleted the live
        # rows permanently. Sweep must run on the full parsed list first.
        from kiro_crew.dashboard.state import (
            _MAX_PERSISTED_NOTIFICATIONS,
            _load_notifications,
            _persist_notification,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        live = _MAX_PERSISTED_NOTIFICATIONS
        for i in range(live):
            _persist_notification(_note(title=f"live-{i}"))
        # 50 newer expired-passive rows push the file over the cap
        for i in range(50):
            _persist_notification(
                _note(priority="passive", ttl=60, ts=str(time.time() - 120))
            )
        loaded = _load_notifications()
        titles = {n.get("title") for n in loaded}
        assert len(loaded) == live  # every live row survives
        assert "live-0" in titles and f"live-{live - 1}" in titles

    def test_expired_passive_note_removed(self):
        log = [_note(priority="passive", ttl=60, ts=str(time.time() - 120))]
        assert sweep_expired_notifications(log) == 1
        assert log == []

    def test_unexpired_passive_note_kept(self):
        log = [_note(priority="passive", ttl=3600, ts=str(time.time() - 60))]
        assert sweep_expired_notifications(log) == 0
        assert len(log) == 1

    def test_non_passive_note_with_ttl_kept(self):
        # Only passive notes sweep -- critical/default history has recall value.
        log = [
            _note(priority="critical", ttl=1, ts=str(time.time() - 100)),
            _note(priority="default", ttl=1, ts=str(time.time() - 100)),
        ]
        assert sweep_expired_notifications(log) == 0
        assert len(log) == 2

    def test_passive_note_without_ttl_kept(self):
        log = [_note(priority="passive", ts=str(time.time() - 10**6))]
        assert sweep_expired_notifications(log) == 0

    def test_iso_timestamp_supported(self):
        old_iso = datetime.fromtimestamp(
            time.time() - 120, tz=timezone.utc
        ).isoformat()
        log = [_note(priority="passive", ttl=60, ts=old_iso)]
        assert sweep_expired_notifications(log) == 1

    def test_unparseable_ts_kept(self):
        # Never destroy on ambiguity.
        log = [_note(priority="passive", ttl=1, ts="not-a-timestamp")]
        assert sweep_expired_notifications(log) == 0

    @pytest.mark.parametrize(
        "poison_ts",
        [
            "0001-01-01T00:00:00",  # pre-epoch: .timestamp() OSError/OverflowError on Windows
            "9999-12-31T23:59:59",  # far-future: same class of platform overflow
            "-1e999",  # float() returns -inf WITHOUT raising: every TTL reads expired
            "1e999",  # +inf: ttl < now - inf is never true, but must still be kept-as-ambiguous
            "nan",  # NaN: no ordering meaning at all
        ],
    )
    def test_platform_unrepresentable_ts_kept_and_never_raises(self, poison_ts):
        # Arbiter finding on PR #422: OverflowError/OSError escaping the
        # sweep at load time hits _load_notifications' blanket handler and
        # EMPTIES the entire history (persisted on the next mutation). The
        # poison row must be kept and the healthy rows must survive.
        # GPT 5.6 round 23: non-finite float STRINGS ("-1e999") don't raise --
        # float() returns -inf and the sweep would DELETE the row. Non-finite
        # epochs must be treated as unparseable (kept), same as ISO poison.
        log = [
            _note(priority="passive", ttl=1, ts=poison_ts),
            _note(priority="passive", ttl=60, ts=str(time.time() - 120), title="expired"),
            _note(title="healthy"),
        ]
        removed = sweep_expired_notifications(log)
        titles = [n["title"] for n in log]
        assert "t" in titles  # poison row kept
        assert "healthy" in titles
        assert removed == 1  # the genuinely expired row still sweeps
        assert "expired" not in titles

    def test_bool_ttl_not_treated_as_int(self):
        # bool is an int subclass; True must not make a note expire.
        log = [_note(priority="passive", ttl=True, ts=str(time.time() - 100))]
        assert sweep_expired_notifications(log) == 0

    def test_delivery_sweeps_lazily(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._notification_log.append(
            _note(priority="passive", ttl=60, ts=str(time.time() - 120))
        )
        state._deliver_note(_note(title="fresh"))
        titles = [n["title"] for n in state._notification_log]
        assert titles == ["fresh"]


class TestSendNotificationToolIdentity:
    """GPT 5.6 HIGH round 7: the MCP tool must resolve identity STRICTLY --
    the lenient resolver accepts forgeable pid files, and the key selects the
    publish authorization and audit attribution."""

    def test_fails_closed_without_verified_identity(self, monkeypatch):
        from kiro_crew import mcp_core

        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        posted: list = []
        monkeypatch.setattr(mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True})
        result = mcp_core._call_tool_inner("send_notification", {"title": "t"})
        assert "cannot verify caller identity" in result
        assert not posted  # nothing published

    def test_verified_identity_travels_in_payload(self, monkeypatch):
        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9"
        )
        monkeypatch.setattr(
            mcp_core, "_vet_messaging_governance", lambda *a, **k: None
        )
        posted: list = []

        def fake_post(path, payload):
            posted.append((path, payload))
            return {"ok": True, "note": {"channel": "system.agent", "priority": "default"}}

        monkeypatch.setattr(mcp_core, "_post", fake_post)
        result = mcp_core._call_tool_inner("send_notification", {"title": "t"})
        assert "published" in result
        assert posted[0][0] == "/api/notifications/agent"
        # Identity authorizes the publish and attributes audits; it is NOT
        # forwarded in the payload (no gateway-side consumer).
        assert "session_key" not in posted[0][1]

    def test_gateway_failure_returns_error_prefix(self, monkeypatch):
        # GPT 5.6 round 12 (MEDIUM): call_tool_with_logging classifies only
        # "Error:"-prefixed strings as failures -- a "Failed:" return was
        # SEL-recorded as completed, contradicting the actual outcome.
        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9"
        )
        monkeypatch.setattr(
            mcp_core, "_vet_messaging_governance", lambda *a, **k: None
        )
        monkeypatch.setattr(
            mcp_core, "_post", lambda *a, **k: {"ok": False, "error": "boom"}
        )
        result = mcp_core._call_tool_inner("send_notification", {"title": "t"})
        assert result.startswith("Error:")

    def test_governance_allowed_sel_record_on_permit(self, monkeypatch):
        # GPT 5.6 round 13 (HIGH): the shared helper must audit ALLOWED
        # decisions too, not only denials (backend-security-controls).
        from kiro_crew import mcp_core

        class Allow:
            permitted = True
            rule = "default"
            layer = "policy"
            reason = ""

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *a, **k: Allow(),
        )
        audits: list[dict] = []
        sel_mock = MagicMock()
        sel_mock.log_governance_decision = lambda **kw: audits.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
        assert (
            mcp_core._vet_messaging_governance(
                "dashboard:chat-9", tool_name="send_notification"
            )
            is None
        )
        assert audits and audits[0]["outcome"] == "allowed"
        assert audits[0]["tool_name"] == "send_notification"
        assert audits[0]["scope"] == "capabilities.messaging"

    def test_governance_denial_sel_attributes_to_send_notification(self, monkeypatch):
        # Arbiter round 9 (GPT 5.6 MEDIUM): the shared messaging-governance
        # helper hardcoded tool_name="send_message" in its SEL denial record,
        # misattributing every governance-denied send_notification call in the
        # persisted audit trail.
        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9"
        )

        class Deny:
            permitted = False
            rule = "no-messaging"
            layer = "profile"
            reason = "policy denies messaging"

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *a, **k: Deny(),
        )
        audits: list[dict] = []
        sel_mock = MagicMock()
        sel_mock.log_governance_decision = lambda **kw: audits.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
        result = mcp_core._call_tool_inner("send_notification", {"title": "t"})
        assert "blocked by governance" in result
        assert audits and audits[0]["outcome"] == "denied"
        assert audits[0]["tool_name"] == "send_notification"

    def test_governance_denial_sel_still_names_send_message(self, monkeypatch):
        # Companion pin: the helper's default keeps send_message attribution.
        from kiro_crew import mcp_core

        class Deny:
            permitted = False
            rule = "no-messaging"
            layer = "profile"
            reason = "policy denies messaging"

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *a, **k: Deny(),
        )
        audits: list[dict] = []
        sel_mock = MagicMock()
        sel_mock.log_governance_decision = lambda **kw: audits.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
        denial = mcp_core._vet_messaging_governance("dashboard:chat-9")
        assert denial is not None
        assert audits and audits[0]["tool_name"] == "send_message"

    def test_governance_error_fails_closed_for_send_notification(self, monkeypatch):
        # GPT 5.6 HIGH round 10: governance_permits defaults to fail-open on
        # evaluation error, so a malformed profile or governance read failure
        # let send_notification bypass a configured messaging denial. The
        # notification path must deny on error (deny-by-default backend rule).
        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9"
        )

        def _boom(*a, **k):
            raise RuntimeError("governance backend down")

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits", _boom
        )
        posted: list = []
        monkeypatch.setattr(
            mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True}
        )
        result = mcp_core._call_tool_inner("send_notification", {"title": "t"})
        assert "fail-closed" in result
        assert not posted  # nothing published on governance error

    def test_governance_error_still_degrades_open_for_send_message(self, monkeypatch):
        # Companion pin: send_message keeps its documented best-effort
        # (degrade-open) posture -- only the notification path fails closed.
        from kiro_crew import mcp_core

        def _boom(*a, **k):
            raise RuntimeError("governance backend down")

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits", _boom
        )
        assert mcp_core._vet_messaging_governance("dashboard:chat-9") is None


class TestAgentEndpointAuthWiring:
    """Opus HIGH on PR #422: the route must be on the internal-secret
    allowlist or every MCP call falls through to cookie auth and 403s."""

    def test_agent_path_in_strict_internal_allowlist(self):
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

        assert "/api/notifications/agent" in _STRICT_INTERNAL_API_PATHS

    @pytest.mark.asyncio
    async def test_middleware_admits_internal_secret_on_agent_path(self):
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        async def ok(_request):
            return web.Response(text="ok")

        secret = "test-secret-123"
        mw = token_auth_middleware(
            internal_paths=_STRICT_INTERNAL_API_PATHS, internal_secret=secret
        )
        req = MagicMock(spec=web.Request)
        req.path = "/api/notifications/agent"
        req.query = {}
        req.cookies = {}
        req.remote = "127.0.0.1"
        req.headers = {"X-Internal-Secret": secret}
        req.method = "POST"
        resp = await mw(req, ok)
        assert resp.status == 200


class TestAgentPushEndpoint:
    def _app(self, state, *, internal_auth: bool = True) -> web.Application:
        # internal_auth=True mirrors the production transport: the tool's
        # X-Internal-Secret path is the only one that reaches this handler
        # (the middleware sets request["internal_auth"] on it). Pass False
        # to simulate a loopback dashboard-cookie caller.
        @web.middleware
        async def _auth_marker(request, handler):
            if internal_auth:
                request["internal_auth"] = True
            return await handler(request)

        app = web.Application(middlewares=[_auth_marker])
        app["state"] = state
        app.router.add_post("/api/notifications/agent", api_notification_agent_push)
        return app

    @pytest.mark.asyncio
    async def test_cookie_callers_denied(self, monkeypatch, tmp_path):
        # GPT 5.6 HIGH round 19: the strict-internal middleware also admits
        # loopback dashboard-cookie callers — a browser-credentialed caller
        # publishing source="system" would bypass MCP governance. The
        # handler requires the validated internal-secret marker.
        state = _make_state(monkeypatch, tmp_path)
        sel_mock = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging._sel", lambda: sel_mock
        )
        async with TestClient(
            TestServer(self._app(state, internal_auth=False))
        ) as client:
            resp = await client.post(
                "/api/notifications/agent", json={"title": "cookie spoof"}
            )
            assert resp.status == 403
        assert not any(
            n.get("title") == "cookie spoof" for n in state._notification_log
        )
        denied = [
            kw
            for _, kw in sel_mock.log_api_access.call_args_list
            if kw.get("outcome") == "denied"
        ]
        assert denied

    @pytest.mark.asyncio
    async def test_oversized_body_rejected_before_decoding(
        self, monkeypatch, tmp_path
    ):
        # GPT 5.6 round 11 (MEDIUM): without an endpoint cap the route
        # inherits the server-wide client_max_size and decodes megabytes on
        # the event-loop thread. Mirror the app push endpoint's 64 KB bound.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "t", "body": "x" * (70 * 1024)},
            )
            assert resp.status == 413

    @pytest.mark.asyncio
    async def test_publishes_through_system_agent_channel(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "Job done", "body": "42 rows", "priority": "passive"},
            )
            assert resp.status == 200
            data = await resp.json()
            note = data["note"]
            assert note["channel"] == "system.agent"
            assert note["source"] == "system"
            assert note["priority"] == "passive"
        assert state._notification_log[-1]["title"] == "Job done"

    @pytest.mark.asyncio
    async def test_channel_and_source_are_server_fixed(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "t", "channel": "system.approval", "source": "evil"},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
            assert note["channel"] == "system.agent"
            assert note["source"] == "system"

    @pytest.mark.asyncio
    async def test_validation_errors_return_400(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            for bad in (
                {"title": ""},  # title required
                {"title": "t", "priority": "urgent"},  # unknown priority
                {"title": "t", "url": "https://evil.example.com"},  # external url
            ):
                resp = await client.post("/api/notifications/agent", json=bad)
                assert resp.status == 400, bad

    @pytest.mark.asyncio
    async def test_non_object_body_returns_400(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/notifications/agent", json=["not", "obj"])
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_string_and_non_list_fields_return_400_not_500(
        self, monkeypatch, tmp_path
    ):
        # GPT 5.6 MEDIUM round 3: wrong-typed fields raised AttributeError/
        # TypeError past the validation catch -- a 500 where the contract
        # says 400.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(self._app(state))) as client:
            for bad in (
                {"title": "t", "url": 42},
                {"title": "t", "url": ["x"]},
                {"title": "t", "actions": {}},
                {"title": "t", "group_key": 7},
                {"title": ["t"]},
            ):
                resp = await client.post("/api/notifications/agent", json=bad)
                assert resp.status == 400, bad

    @pytest.mark.asyncio
    async def test_app_token_callers_denied(self, monkeypatch, tmp_path):
        # GPT 5.6 HIGH round 16: app-token API permissions use prefix-boundary
        # matching, so an app allowed "/api/notifications" is admitted to
        # this child route by the middleware. The handler must refuse app
        # identities -- this publish path emits source="system" and can
        # bypass its rate limits, so an app reaching it would
        # impersonate system notifications past its rate limits.
        state = _make_state(monkeypatch, tmp_path)
        sel_mock = MagicMock()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging._sel", lambda: sel_mock
        )

        @web.middleware
        async def _fake_app_token(request, handler):
            request["app"] = "some-app"  # what app-token auth sets
            return await handler(request)

        app = web.Application(middlewares=[_fake_app_token])
        app["state"] = state
        app.router.add_post("/api/notifications/agent", api_notification_agent_push)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/notifications/agent", json={"title": "spoofed system note"}
            )
            assert resp.status == 403
        assert not any(
            n.get("title") == "spoofed system note" for n in state._notification_log
        )
        # GPT 5.6 round 18 HIGH: the denial is a permission decision on a
        # security boundary — it must land in the SEL audit trail.
        denied = [
            kw
            for _, kw in sel_mock.log_api_access.call_args_list
            if kw.get("outcome") == "denied"
        ]
        assert denied and denied[0]["caller"] == "app:some-app"

    @pytest.mark.asyncio
    async def test_persist_failure_returns_500(self, monkeypatch, tmp_path):
        # GPT 5.6 HIGH round 16: the endpoint's durability guarantee -- a 200
        # is only returned once the persist job succeeded. A failed persist
        # must surface as a 500, never a silent acknowledgment.
        state = _make_state(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.state._persist_notification", lambda note: False
        )
        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent", json={"title": "must not ack"}
            )
            assert resp.status == 500
            assert "persist" in (await resp.json())["error"]


class TestSendNotificationSchemaRegistry:
    """The MCP-facing validation registry entry (GPT 5.6 HIGH round 16).

    Tool-level tests elsewhere call ``_call_tool_inner`` directly, bypassing
    ``_validate_args``; this pins the registry wiring so malformed MCP input
    (e.g. ``{}`` without the required title) is rejected with an ``Error:``
    response instead of reaching ``args["title"]`` and raising ``KeyError``
    on the stdio request path.
    """

    def test_missing_title_rejected_before_tool_body(self):
        from unittest.mock import patch as _patch

        from kiro_crew import mcp_core

        with _patch("kiro_crew.mcp_core._post") as mock_post:
            result = mcp_core._call_tool("send_notification", {})
        assert result.startswith("Error:")
        mock_post.assert_not_called()


class TestRound17Fixes:
    """GPT 5.6 round 17: warm-pool identity via caller context, channel-agent
    dispatch containment, and the in-memory notification-log bound."""

    def test_strict_resolver_accepts_gateway_caller_context(self, monkeypatch):
        # Warm-pool on macOS/Windows: the backend process has neither
        # KIROCREW_SESSION_KEY nor KIROCREW_HOST_PID. The gateway-injected
        # per-call caller context (pooled topology) must resolve instead of
        # failing closed.
        from kiro_crew import mcp_caller, mcp_core

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        assert mcp_core._resolve_session_key_strict() == ""
        ctx = mcp_caller.CallerContext(
            session_key="dashboard:chat-7", from_gateway=True
        )
        mcp_caller.set_current_caller(ctx)
        try:
            assert mcp_core._resolve_session_key_strict() == "dashboard:chat-7"
            assert mcp_core._resolve_session_key() == "dashboard:chat-7"
        finally:
            mcp_caller.set_current_caller(None)

    def test_warm_pool_send_notification_publishes_via_caller_context(
        self, monkeypatch
    ):
        # End-to-end warm-pool shape: no env identity at all, caller context
        # installed by the dispatch loop -> the tool publishes with it.
        from kiro_crew import mcp_caller, mcp_core

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        monkeypatch.setattr(mcp_core, "_vet_messaging_governance", lambda *a, **k: None)
        posted: list = []

        def fake_post(path, payload):
            posted.append((path, payload))
            return {"ok": True, "note": {"channel": "system.agent", "priority": "default"}}

        monkeypatch.setattr(mcp_core, "_post", fake_post)
        mcp_caller.set_current_caller(
            mcp_caller.CallerContext(session_key="dashboard:chat-42", from_gateway=True)
        )
        try:
            result = mcp_core._call_tool_inner("send_notification", {"title": "t"})
        finally:
            mcp_caller.set_current_caller(None)
        assert "published" in result
        assert "session_key" not in posted[0][1]

    @pytest.mark.parametrize("tool", ["send_message", "send_notification"])
    def test_channel_agent_denied_at_mcp_dispatch(self, monkeypatch, tool):
        # GPT 5.6 round 17 HIGH: auto-approved kirocrew-core calls emit no
        # permission event, so channel.py's guard never fires — the
        # containment boundary must hold at MCP dispatch on the verified
        # caller identity.
        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "_resolve_session_key", lambda: "channel:C1:agent-1"
        )
        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "channel:C1:agent-1"
        )
        monkeypatch.setattr(mcp_core, "_vet_messaging_governance", lambda *a, **k: None)
        monkeypatch.setattr(mcp_core, "_vet_channel_governance", lambda *a, **k: None)
        audits: list = []
        sel_mock = MagicMock()
        sel_mock.log_tool_invocation = lambda **kw: audits.append(kw)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
        posted: list = []
        monkeypatch.setattr(
            mcp_core, "_post", lambda *a, **k: posted.append(a) or {"ok": True}
        )
        args = {"text": "hi"} if tool == "send_message" else {"title": "hi"}
        result = mcp_core._call_tool_inner(tool, args)
        assert result.startswith("Error:")
        assert "channel agents" in result
        assert not posted
        assert any(a.get("outcome") == "rejected_blocked_tool" for a in audits)

    def test_in_memory_log_bounded_on_live_delivery(self, monkeypatch, tmp_path):
        # GPT 5.6 round 17 MEDIUM: only the disk-load path capped the
        # in-memory list; sustained live deliveries grew it without bound
        # (and the per-delivery sweep scans it -> O(N^2) delivery).
        from kiro_crew.dashboard import state as state_mod

        state = _make_state(monkeypatch, tmp_path)
        cap = state_mod._MAX_PERSISTED_NOTIFICATIONS
        for i in range(cap + 25):
            state._deliver_note(_note(title=f"n{i}", priority="passive"))
        assert len(state._notification_log) == cap
        # Newest rows survive; the oldest 25 were dropped.
        assert state._notification_log[-1]["title"] == f"n{cap + 24}"
        assert state._notification_log[0]["title"] == "n25"

    def test_huge_integer_ts_does_not_abort_sweep(self):
        # GPT 5.6 round 18 MEDIUM: float() of a JSON integer beyond float
        # range raises OverflowError (unlike a float literal, which becomes
        # inf) — one poison row must not abort the sweep and retain every
        # expired note.
        now = time.time()
        rows = [
            {"ts": 10**400, "priority": "passive", "ttl": 60},  # poison
            {
                "ts": now - 3600,
                "priority": "passive",
                "ttl": 60,
            },  # genuinely expired
        ]
        removed = sweep_expired_notifications(rows)
        assert removed == 1
        assert len(rows) == 1
        assert rows[0]["ts"] == 10**400  # poison row kept (never destroyed)

    def test_actions_forwarded_to_endpoint(self, monkeypatch):
        # GPT 5.6 round 20 (MEDIUM): the endpoint documents inline actions
        # but the tool schema omitted them -- a schema-valid MCP call could
        # never carry the Phase 4 action contract.
        from kiro_crew import mcp_core

        monkeypatch.setattr(
            mcp_core, "_resolve_session_key_strict", lambda: "dashboard:chat-9"
        )
        monkeypatch.setattr(mcp_core, "_vet_messaging_governance", lambda *a, **k: None)
        posted: list = []

        def fake_post(path, payload):
            posted.append((path, payload))
            return {"ok": True, "note": {"channel": "system.agent", "priority": "default"}}

        monkeypatch.setattr(mcp_core, "_post", fake_post)
        actions = [{"id": "open", "label": "Open run", "url": "/runs/7"}]
        result = mcp_core._call_tool(
            "send_notification", {"title": "t", "actions": actions}
        )
        assert "published" in result
        assert posted[0][1]["actions"] == actions

    def test_actions_wrong_type_rejected_by_schema(self, monkeypatch):
        from unittest.mock import patch as _patch

        from kiro_crew import mcp_core

        with _patch("kiro_crew.mcp_core._post") as mock_post:
            result = mcp_core._call_tool(
                "send_notification", {"title": "t", "actions": "not-a-list"}
            )
        assert result.startswith("Error:")
        mock_post.assert_not_called()

    def test_action_unknown_keys_rejected(self):
        # GPT 5.6 HIGH round 21: _redact_note_value scrubs VALUES, not dict
        # KEYS -- a credential smuggled as an extra property NAME would reach
        # JSONL and dashboard responses unredacted. The contract is a closed
        # {id, label, url?} set.
        from kiro_crew.notifications.bus import (
            NotificationPayload,
            NotificationValidationError,
        )

        payload = NotificationPayload(
            source="system",
            channel="system.agent",
            kind="agent",
            title="t",
            body="",
            actions=[{"id": "a", "label": "Go", "AKIA_SECRET_IN_KEY": "x"}],
        )
        with pytest.raises(NotificationValidationError, match="unknown fields"):
            payload.validate()
        # The legal shape still passes.
        ok = NotificationPayload(
            source="system",
            channel="system.agent",
            kind="agent",
            title="t",
            body="",
            actions=[{"id": "a", "label": "Go", "url": "/runs/1"}],
        )
        ok.validate()
