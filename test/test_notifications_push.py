"""Tests for Phase 2 of the local notification bus RFC: app producers."""

import asyncio
import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manifest import (
    MAX_NOTIFICATION_CHANNELS,
    AppManifest,
    NotificationChannel,
    NotificationsConfig,
)
from kiro_crew.dashboard.handlers.notifications_push import api_push_notification
from kiro_crew.dashboard.token_auth import app_token_path_allowed
from kiro_crew.notifications.bus import NotificationBus
from kiro_crew.notifications.rate_limit import RATE_LIMIT_BURST, AppRateLimiter

# ── Manifest channel declarations ──


class TestNotificationsManifest:
    def _manifest(self, channels: list[NotificationChannel]) -> AppManifest:
        return AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test App",
            description="d",
            notifications=NotificationsConfig(channels=channels),
        )

    def test_valid_channels_pass(self):
        m = self._manifest(
            [
                NotificationChannel(id="ticket-update", name="Ticket Updates"),
                NotificationChannel(id="alerts", name="Alerts", defaultPriority="critical"),
            ]
        )
        assert m.validate() == []

    def test_channel_cap_enforced(self):
        channels = [
            NotificationChannel(id=f"ch-{i}", name=f"Channel {i}")
            for i in range(MAX_NOTIFICATION_CHANNELS + 1)
        ]
        errors = self._manifest(channels).validate()
        assert any("at most" in e for e in errors)

    def test_exactly_at_cap_passes(self):
        channels = [
            NotificationChannel(id=f"ch-{i}", name=f"Channel {i}")
            for i in range(MAX_NOTIFICATION_CHANNELS)
        ]
        assert self._manifest(channels).validate() == []

    def test_non_kebab_id_rejected(self):
        errors = self._manifest([NotificationChannel(id="Bad_Id", name="Bad")]).validate()
        assert any("kebab-case" in e for e in errors)

    def test_duplicate_id_rejected(self):
        errors = self._manifest(
            [
                NotificationChannel(id="dup", name="A"),
                NotificationChannel(id="dup", name="B"),
            ]
        ).validate()
        assert any("duplicate" in e for e in errors)

    def test_bad_priority_rejected(self):
        errors = self._manifest(
            [NotificationChannel(id="ch", name="C", defaultPriority="urgent")]
        ).validate()
        assert any("defaultPriority" in e for e in errors)

    def test_missing_name_rejected(self):
        errors = self._manifest([NotificationChannel(id="ch")]).validate()
        assert any("missing name" in e for e in errors)

    def test_round_trip_through_dict(self):
        m = self._manifest(
            [NotificationChannel(id="ch", name="C", icon="bell", defaultPriority="passive")]
        )
        restored = AppManifest.from_dict(m.to_dict())
        assert restored.notifications.channels[0].id == "ch"
        assert restored.notifications.channels[0].icon == "bell"
        assert restored.notifications.channels[0].defaultPriority == "passive"

    def test_no_channels_serializes_empty(self):
        m = self._manifest([])
        assert "notifications" not in m.to_dict()


# ── Rate limiter ──


class TestAppRateLimiter:
    def test_burst_allowed_then_limited(self):
        limiter = AppRateLimiter()
        for _ in range(RATE_LIMIT_BURST):
            assert limiter.allow("app-a")
        assert not limiter.allow("app-a")

    def test_apps_have_independent_buckets(self):
        limiter = AppRateLimiter()
        for _ in range(RATE_LIMIT_BURST):
            limiter.allow("app-a")
        assert not limiter.allow("app-a")
        assert limiter.allow("app-b")

    def test_tokens_refill_over_time(self, monkeypatch):
        limiter = AppRateLimiter()
        now = [1000.0]
        monkeypatch.setattr(
            "kiro_crew.notifications.rate_limit.time.monotonic", lambda: now[0]
        )
        for _ in range(RATE_LIMIT_BURST):
            assert limiter.allow("app-a")
        assert not limiter.allow("app-a")
        now[0] += 60.0  # 60s at 0.1 tokens/sec = 6 tokens
        for _ in range(6):
            assert limiter.allow("app-a")
        assert not limiter.allow("app-a")

    def test_refund_returns_token_capped_at_burst(self):
        limiter = AppRateLimiter()
        for _ in range(RATE_LIMIT_BURST):
            assert limiter.allow("app-a")
        assert not limiter.allow("app-a")
        limiter.refund("app-a")
        assert limiter.allow("app-a")  # refunded token usable
        # Refund never exceeds burst, and refunding an unknown app is a no-op.
        fresh = AppRateLimiter()
        fresh.refund("never-seen")
        for _ in range(RATE_LIMIT_BURST):
            assert fresh.allow("never-seen")
        assert not fresh.allow("never-seen")


# ── Token path grant ──


class TestAppTokenPathGrant:
    def test_push_path_allowed_for_app_tokens(self):
        assert app_token_path_allowed("some-app", "/api/notifications/push")

    def test_notification_read_path_still_denied(self):
        assert not app_token_path_allowed("some-app", "/api/notifications")

    def test_empty_app_name_denied(self):
        assert not app_token_path_allowed("", "/api/notifications/push")


# ── Push handler ──


class _FakeState:
    def __init__(self) -> None:
        self.delivered: list[dict] = []
        self.notification_bus = NotificationBus(sink=self.delivered.append)
        self.notification_rate_limiter = AppRateLimiter()
        # None = last persist ran inline (no durability future to await),
        # matching DashboardState's sync-context behavior.
        self.last_notification_persist = None


def _make_app(state: _FakeState, identity: dict) -> web.Application:
    @web.middleware
    async def fake_auth(request, handler):
        if identity["app"]:
            request["app"] = identity["app"]
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    app["state"] = state
    app.router.add_post("/api/notifications/push", api_push_notification)
    return app


_CHANNELS = {"ticket-update": "default", "alerts": "critical"}


def _patch_channels(channels="unset"):
    return patch(
        "kiro_crew.dashboard.handlers.notifications_push._resolve_app_channels",
        return_value=dict(_CHANNELS) if channels == "unset" else channels,
    )


def _fresh_limiter():
    # The limiter is state-owned (each _FakeState gets its own), so no
    # module-global patching is needed; kept as a no-op context manager so
    # existing test bodies read unchanged.
    return contextlib.nullcontext()


class TestPushHandler:
    @pytest.mark.asyncio
    async def test_push_delivers_with_server_resolved_source(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={
                        "channel": "ticket-update",
                        "title": "New Sev-2",
                        "body": "V123 assigned",
                        "source": "app:EVIL",  # must be ignored
                    },
                )
        assert resp.status == 200
        assert len(state.delivered) == 1
        note = state.delivered[0]
        assert note["source"] == "app:oncall-radar"
        assert note["channel"] == "oncall-radar.ticket-update"
        assert note["kind"] == "ticket-update"

    @pytest.mark.asyncio
    async def test_channel_default_priority_applied(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                await client.post(
                    "/api/notifications/push",
                    json={"channel": "alerts", "title": "t", "body": "b"},
                )
        assert state.delivered[0]["priority"] == "critical"

    @pytest.mark.asyncio
    async def test_undeclared_channel_rejected(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "not-declared", "title": "t", "body": "b"},
                )
        assert resp.status == 400
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_disabled_or_unknown_app_rejected(self):
        state = _FakeState()
        with _patch_channels(channels=None), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 403
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_no_app_identity_rejected(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": ""}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 403
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_rate_limit_returns_429(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                statuses = []
                for _ in range(RATE_LIMIT_BURST + 1):
                    resp = await client.post(
                        "/api/notifications/push",
                        json={"channel": "ticket-update", "title": "t", "body": "b"},
                    )
                    statuses.append(resp.status)
        assert statuses[:RATE_LIMIT_BURST] == [200] * RATE_LIMIT_BURST
        assert statuses[-1] == 429
        assert len(state.delivered) == RATE_LIMIT_BURST

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    data=b"not json",
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status == 400
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_invalid_payload_field_rejected(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={
                        "channel": "ticket-update",
                        "title": "t",
                        "body": "b",
                        "url": "https://evil.example.com/",
                    },
                )
        assert resp.status == 400
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_json_body_must_be_object(self):
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    data=json.dumps([1, 2]),
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status == 400
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_oversized_chunked_body_rejected(self):
        # Regression: chunked transfer-encoding carries no Content-Length, so
        # the header-based size check alone would let an oversized body
        # through to the JSON parser.
        state = _FakeState()

        async def gen():
            yield b'{"channel": "ticket-update", "title": "t", "body": "'
            yield b"x" * (64 * 1024)
            yield b'"}'

        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    data=gen(),
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status == 413
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_falsy_group_key_preserved(self):
        # Regression: group_key 0 is a valid grouping identifier -- a
        # truthiness guard would silently drop it to None.
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b", "group_key": 0},
                )
        assert resp.status == 200
        assert state.delivered[0]["group_key"] == "0"

    @pytest.mark.asyncio
    async def test_explicit_empty_url_rejected_not_dropped(self):
        # Regression: url "" is a client bug -- it must fail validation with
        # 400 rather than be silently treated as absent.
        state = _FakeState()
        with _patch_channels(), _fresh_limiter():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b", "url": ""},
                )
        assert resp.status == 400
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_rate_limiter_is_state_owned_not_module_global(self):
        # Regression: two gateway states must not share rate-limit budgets
        # (the limiter was previously a module-level singleton).
        state_a = _FakeState()
        state_b = _FakeState()
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state_a, {"app": "oncall-radar"}))) as ca:
                for _ in range(RATE_LIMIT_BURST):
                    resp = await ca.post(
                        "/api/notifications/push",
                        json={"channel": "ticket-update", "title": "t", "body": "b"},
                    )
                    assert resp.status == 200
                resp = await ca.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
                assert resp.status == 429
            async with TestClient(TestServer(_make_app(state_b, {"app": "oncall-radar"}))) as cb:
                resp = await cb.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_body_size_boundary_exact(self):
        # Regression: prove the cap boundary is exact for chunked bodies --
        # exactly _MAX_BODY_BYTES must pass the size gate (it then fails
        # payload validation with 400 because the body field exceeds its own
        # cap, which proves the 413 gate was cleared), one byte more -> 413.
        from kiro_crew.dashboard.handlers.notifications_push import _MAX_BODY_BYTES

        prefix = b'{"channel": "ticket-update", "title": "t", "body": "'
        suffix = b'"}'
        filler = _MAX_BODY_BYTES - len(prefix) - len(suffix)

        async def gen(extra: int):
            yield prefix
            yield b"x" * (filler + extra)
            yield suffix

        state = _FakeState()
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                at_cap = await client.post(
                    "/api/notifications/push",
                    data=gen(0),
                    headers={"Content-Type": "application/json"},
                )
                over_cap = await client.post(
                    "/api/notifications/push",
                    data=gen(1),
                    headers={"Content-Type": "application/json"},
                )
        assert at_cap.status != 413
        assert over_cap.status == 413

    def test_resolve_app_channels_is_read_only(self):
        # Regression: _resolve_app_channels runs in a worker thread, so it
        # must never call get_app (its version-sync write to installed.json
        # would race loop-side writers).
        from kiro_crew.dashboard.handlers.notifications_push import _resolve_app_channels

        with patch("kiro_crew.apps.manager.get_app") as get_app_mock, patch(
            "kiro_crew.dashboard.handlers.notifications_push.is_app_enabled",
            return_value=False,
        ):
            assert _resolve_app_channels("some-app") is None
        get_app_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_app_token_denial_emits_sel_audit(self):
        # Regression: every denial path must emit a SEL audit event -- the
        # no-app-token 403 previously skipped it.
        state = _FakeState()
        with patch(
            "kiro_crew.dashboard.handlers.notifications_push.sel"
        ) as sel_mock:
            async with TestClient(TestServer(_make_app(state, {"app": ""}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 403
        calls = sel_mock.return_value.log_api_access.call_args_list
        assert len(calls) == 1
        assert calls[0].kwargs["outcome"] == "denied"
        assert calls[0].kwargs["error"] == "app token required"

    @pytest.mark.asyncio
    async def test_invalid_payload_does_not_consume_rate_token(self):
        # Regression: the rate limiter caps DELIVERED notifications -- invalid
        # payloads (400) must not drain the budget and block later retries.
        state = _FakeState()
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                for _ in range(RATE_LIMIT_BURST + 3):
                    resp = await client.post(
                        "/api/notifications/push",
                        json={"channel": "ticket-update", "title": "t", "body": "b",
                              "url": "https://evil.example.com/"},
                    )
                    assert resp.status == 400
                # Budget untouched: a valid push still succeeds.
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 200
        assert len(state.delivered) == 1

    @pytest.mark.asyncio
    async def test_corrupt_manifest_priority_returns_400_not_500(self):
        # Regression: a corrupt on-disk manifest defaultPriority previously
        # raised out of register_channel unhandled (500).
        state = _FakeState()
        with _patch_channels(channels={"ticket-update": "not-a-priority"}):
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 400
        assert state.delivered == []

    @pytest.mark.asyncio
    async def test_register_once_does_not_stomp_runtime_priority_override(self):
        # Regression: re-registering on every push would reset a runtime
        # per-channel priority override (RFC Phase 3) back to the manifest
        # default. Registration must be first-push-only -- assert the call
        # count explicitly rather than relying on the priority side-effect.
        state = _FakeState()
        register_spy = MagicMock(wraps=state.notification_bus.register_channel)
        state.notification_bus.register_channel = register_spy  # type: ignore[method-assign]
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
                # Simulate a Phase 3 runtime override (direct bus call, not
                # via the spy -- the spy wraps the same underlying method).
                register_spy("oncall-radar.ticket-update", "critical")
                await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t2", "body": "b"},
                )
        # Explicit call-args assertion: the handler registered exactly once
        # (first push, manifest default); the only other call is the test's
        # own override. A handler that re-registered on the second push would
        # append a third ("default") call AND flip the delivered priority.
        assert register_spy.call_args_list == [
            (("oncall-radar.ticket-update", "default"), {}),
            (("oncall-radar.ticket-update", "critical"), {}),
        ]
        assert state.delivered[1]["priority"] == "critical"

    @pytest.mark.asyncio
    async def test_corrupt_manifest_400_does_not_consume_rate_token(self):
        # Regression: registration failure (corrupt manifest priority) is a
        # 400 that delivers nothing -- it must not drain the rate budget.
        state = _FakeState()
        with _patch_channels(channels={"ticket-update": "not-a-priority"}):
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                for _ in range(RATE_LIMIT_BURST + 3):
                    resp = await client.post(
                        "/api/notifications/push",
                        json={"channel": "ticket-update", "title": "t", "body": "b"},
                    )
                    assert resp.status == 400
        # Budget untouched: with a sane channel map the same app still pushes.
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_sink_failure_returns_clean_500_with_sel_audit(self):
        # Regression: a sink failure (e.g. disk full during persist) must be
        # a clean SEL-logged 500, not an unhandled exception.
        state = _FakeState()

        def exploding_sink(note):
            raise OSError("disk full")

        state.notification_bus = NotificationBus(sink=exploding_sink)
        with _patch_channels(), patch(
            "kiro_crew.dashboard.handlers.notifications_push.sel"
        ) as sel_mock:
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
                body = await resp.json()
        assert resp.status == 500
        assert body["error"] == "notification delivery failed"
        outcomes = [c.kwargs.get("outcome") for c in sel_mock.return_value.log_api_access.call_args_list]
        assert "error" in outcomes


class TestPushDurability:
    @pytest.mark.asyncio
    async def test_persist_failure_returns_500_with_sel_audit(self):
        """A 200 is a durability guarantee: when the sink's persist job
        reports failure, the endpoint returns 500 and SEL-audits it (token
        stays spent -- the note WAS broadcast)."""
        state = _FakeState()

        def failing_sink(note):
            state.delivered.append(note)
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(False)  # persist reported failure
            state.last_notification_persist = fut

        state.notification_bus = NotificationBus(sink=failing_sink)
        with _patch_channels(), patch(
            "kiro_crew.dashboard.handlers.notifications_push.sel"
        ) as sel_mock:
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
                body = await resp.json()
        assert resp.status == 500
        assert body["error"] == "notification persistence failed"
        outcomes = [
            c.kwargs.get("outcome")
            for c in sel_mock.return_value.log_api_access.call_args_list
        ]
        assert "error" in outcomes

    @pytest.mark.asyncio
    async def test_success_returns_enriched_note(self):
        """Per the system spec, a 200 carries the enriched note (resolved
        source, full channel, effective priority, server ts)."""
        state = _FakeState()
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body["ok"] is True
        note = body["note"]
        assert note["source"] == "app:oncall-radar"
        assert note["channel"] == "oncall-radar.ticket-update"
        assert note["priority"]
        assert note["ts"]

    @pytest.mark.asyncio
    async def test_durable_persist_success_returns_200(self):
        """When the persist future resolves True, the push succeeds."""
        state = _FakeState()

        def durable_sink(note):
            state.delivered.append(note)
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(True)
            state.last_notification_persist = fut

        state.notification_bus = NotificationBus(sink=durable_sink)
        with _patch_channels():
            async with TestClient(TestServer(_make_app(state, {"app": "oncall-radar"}))) as client:
                resp = await client.post(
                    "/api/notifications/push",
                    json={"channel": "ticket-update", "title": "t", "body": "b"},
                )
        assert resp.status == 200


class TestSigningPayloadCoversNotifications:
    def test_channel_tamper_changes_signing_payload(self):
        # Regression: notifications.channels must be signature-covered --
        # tampering with declarations (e.g. escalating defaultPriority to
        # critical) must invalidate the admission signature.
        base = AppManifest(name="a", version="1.0.0")
        signed = AppManifest(
            name="a", version="1.0.0",
            notifications=NotificationsConfig(
                channels=[NotificationChannel(id="alerts", name="Alerts")]
            ),
        )
        tampered = AppManifest(
            name="a", version="1.0.0",
            notifications=NotificationsConfig(
                channels=[NotificationChannel(id="alerts", name="Alerts", defaultPriority="critical")]
            ),
        )
        assert signed.signing_payload() != base.signing_payload()
        assert tampered.signing_payload() != signed.signing_payload()

    def test_no_channels_keeps_pre_phase2_payload_shape(self):
        # Manifests signed before notifications existed must keep producing
        # the identical payload (no key present when channels are empty).
        m = AppManifest(name="a", version="1.0.0")
        assert b"notifications" not in m.signing_payload()
