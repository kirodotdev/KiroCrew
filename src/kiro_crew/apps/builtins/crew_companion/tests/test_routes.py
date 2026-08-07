"""The HTTP surface and the lifecycle hooks.

The point of these tests is the thing the previous shape got wrong: that enabling
the app is sufficient to make it work. There is no separate process to launch, so
"enabled" and "working" are the same state, and these assertions are what hold
that true.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.crew_companion import hooks
from kiro_crew.apps.builtins.crew_companion.backend import routes
from kiro_crew.apps.builtins.crew_companion.reminders import parse_iso, to_iso
from kiro_crew.apps.builtins.crew_companion.store import CompanionStore

BASE = "/api/apps/crew-companion"
NOW = parse_iso("2026-07-31T14:00:00")


@pytest.fixture(autouse=True)
def _clean_runtime():
    """No test may inherit another's process-global runtime."""
    hooks._reset_for_tests()
    yield
    hooks._reset_for_tests()


@pytest.fixture()
def enabled(monkeypatch):
    """Pretend the app is enabled, without touching installed.json."""
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)


@pytest.fixture()
def disabled(monkeypatch):
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: False)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = CompanionStore(tmp_path, rand=lambda: 0.0, now=lambda: NOW)
    s.load()
    monkeypatch.setattr(hooks, "_store", s)
    return s


async def _client() -> TestClient:
    app = web.Application()
    routes.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ── the enablement gate ─────────────────────────────────────────────────────


class TestGate:
    @pytest.mark.asyncio
    async def test_disabled_answers_403_with_a_machine_readable_code(
        self, disabled, store
    ):
        client = await _client()
        try:
            r = await client.get(f"{BASE}/reminders")
            assert r.status == 403
            body = await r.json()
            assert body["code"] == "app_disabled"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_enabled_but_not_started_answers_503_not_500(
        self, enabled, monkeypatch
    ):
        """'Not up yet' is a different answer from 'broken', and only one of them
        is worth retrying — so the runtime being absent must not read as a crash."""
        monkeypatch.setattr(hooks, "_store", None)
        client = await _client()
        try:
            r = await client.get(f"{BASE}/reminders")
            assert r.status == 503
            assert (await r.json())["code"] == "runtime_not_started"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_enabled_and_started_serves_the_payload(self, enabled, store):
        client = await _client()
        try:
            r = await client.get(f"{BASE}/reminders")
            assert r.status == 200
            body = await r.json()
            # The camelCase shape the existing renderer already expects.
            assert set(body) >= {
                "reminders",
                "breakNudgesEnabled",
                "sessionNotificationsEnabled",
                "breakReminderMins",
            }
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_every_route_is_gated(self, disabled, store):
        """A route added without the guard is the hole this pins shut."""
        client = await _client()
        try:
            for method, path in [
                ("get", f"{BASE}/reminders"),
                ("get", f"{BASE}/stats"),
                ("get", f"{BASE}/pending"),
                ("post", f"{BASE}/reminders/add"),
                ("post", f"{BASE}/reminders/remove"),
                ("post", f"{BASE}/reminders/skip"),
                ("post", f"{BASE}/reminders/config"),
                ("post", f"{BASE}/presence"),
                ("post", f"{BASE}/breathing-done"),
            ]:
                r = await getattr(client, method)(path, json={})
                assert r.status == 403, f"{method.upper()} {path} was not gated"
        finally:
            await client.close()


# ── reminders round trip ────────────────────────────────────────────────────


class TestReminderRoundTrip:
    @pytest.mark.asyncio
    async def test_add_then_list_then_remove(self, enabled, store):
        client = await _client()
        try:
            fire_at = to_iso(NOW + timedelta(hours=1))
            r = await client.post(
                f"{BASE}/reminders/add", json={"text": "drink water", "fireAt": fire_at}
            )
            assert r.status == 200
            ident = (await r.json())["id"]

            rows = (await (await client.get(f"{BASE}/reminders")).json())["reminders"]
            assert [x["text"] for x in rows] == ["drink water"]

            r = await client.post(f"{BASE}/reminders/remove", json={"id": ident})
            assert (await r.json())["ok"] is True
            rows = (await (await client.get(f"{BASE}/reminders")).json())["reminders"]
            assert rows == []
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_recurring_reminder_can_be_skipped(self, enabled, store):
        client = await _client()
        try:
            fire_at = to_iso(NOW + timedelta(minutes=30))
            ident = (
                await (
                    await client.post(
                        f"{BASE}/reminders/add",
                        json={"text": "stretch", "fireAt": fire_at, "everyMinutes": 60},
                    )
                ).json()
            )["id"]

            r = await client.post(f"{BASE}/reminders/skip", json={"id": ident})
            body = await r.json()
            assert body["ok"] is True
            assert parse_iso(body["fireAt"]) > NOW + timedelta(minutes=30)
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_skipping_a_one_time_reminder_is_refused_not_silently_ignored(
        self, enabled, store
    ):
        client = await _client()
        try:
            ident = (
                await (
                    await client.post(
                        f"{BASE}/reminders/add",
                        json={"text": "once", "fireAt": to_iso(NOW + timedelta(hours=2))},
                    )
                ).json()
            )["id"]
            body = await (
                await client.post(f"{BASE}/reminders/skip", json={"id": ident})
            ).json()
            assert body == {"ok": False, "reason": "not-recurring"}
        finally:
            await client.close()


# ── input validation ────────────────────────────────────────────────────────


class TestValidation:
    @pytest.mark.asyncio
    async def test_a_missing_time_is_rejected_rather_than_guessed(self, enabled, store):
        """The one rule the parser must never break is inventing a time the user
        did not give. The backend holds the same line rather than defaulting."""
        client = await _client()
        try:
            r = await client.post(f"{BASE}/reminders/add", json={"text": "no time"})
            assert r.status == 400
            assert (await r.json())["code"] == "fire_at_required"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_empty_text_is_rejected(self, enabled, store):
        client = await _client()
        try:
            r = await client.post(
                f"{BASE}/reminders/add", json={"text": "   ", "fireAt": to_iso(NOW)}
            )
            assert r.status == 400
            assert (await r.json())["code"] == "text_required"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_nonsense_recurrence_is_rejected(self, enabled, store):
        client = await _client()
        try:
            r = await client.post(
                f"{BASE}/reminders/add",
                json={"text": "x", "fireAt": to_iso(NOW), "everyMinutes": -5},
            )
            assert r.status == 400
            assert (await r.json())["code"] == "invalid_recurrence"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_non_finite_recurrence_is_rejected_not_a_500(self, enabled, store):
        """Infinity passes `> 0`, and `int(inf)` then raised an uncaught
        OverflowError — a crafted 1e309 crashed the request with a 500
        instead of a validation reply. JSON has no literal Infinity, but
        1e309 parses to it."""
        client = await _client()
        try:
            r = await client.post(
                f"{BASE}/reminders/add",
                data=b'{"text": "x", "fireAt": "%s", "everyMinutes": 1e309}'
                % to_iso(NOW).encode(),
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 400
            assert (await r.json())["code"] == "invalid_recurrence"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_huge_integer_recurrence_is_rejected_not_a_500(self, enabled, store):
        """A 400-digit integer parses as an exact Python int — it is positive
        and finite, but `math.isfinite` itself raises OverflowError on an int
        too large for a float, so the previous guard crashed on exactly the
        input it was added to reject. The bounded range check rejects it with
        a clean 400."""
        client = await _client()
        try:
            r = await client.post(
                f"{BASE}/reminders/add",
                data=b'{"text": "x", "fireAt": "%s", "everyMinutes": %s}'
                % (to_iso(NOW).encode(), b"9" * 400),
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 400
            assert (await r.json())["code"] == "invalid_recurrence"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_a_bad_cursor_is_rejected(self, enabled, store):
        client = await _client()
        try:
            r = await client.get(f"{BASE}/pending?since=abc")
            assert r.status == 400
            assert (await r.json())["code"] == "invalid_cursor"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_malformed_json_does_not_500(self, enabled, store):
        client = await _client()
        try:
            r = await client.post(
                f"{BASE}/reminders/add",
                data="{not json",
                headers={"Content-Type": "application/json"},
            )
            assert r.status == 400
        finally:
            await client.close()


# ── the delivery endpoint the overlay polls ─────────────────────────────────


class TestPending:
    @pytest.mark.asyncio
    async def test_a_fired_reminder_appears_and_the_cursor_advances(
        self, enabled, store
    ):
        client = await _client()
        try:
            await client.post(
                f"{BASE}/reminders/add",
                json={"text": "water", "fireAt": to_iso(NOW - timedelta(minutes=1))},
            )
            store.tick()

            body = await (await client.get(f"{BASE}/pending?since=0")).json()
            assert [f["kind"] for f in body["fires"]] == ["reminder"]
            assert body["cursor"] >= 1

            # Past the cursor there is nothing new — but the item was not consumed.
            after = await (
                await client.get(f"{BASE}/pending?since={body['cursor']}")
            ).json()
            assert after["fires"] == []
            again = await (await client.get(f"{BASE}/pending?since=0")).json()
            assert len(again["fires"]) == 1
        finally:
            await client.close()


# ── lifecycle hooks ─────────────────────────────────────────────────────────


class TestHooks:
    @pytest.mark.asyncio
    async def test_startup_builds_a_runtime_and_shutdown_stops_it(self, tmp_path):
        ctx = SimpleNamespace(data_dir=str(tmp_path))
        assert hooks.get_store() is None
        await hooks.on_startup(ctx)
        assert hooks.get_store() is not None
        await hooks.on_shutdown(ctx)
        # Deliberately KEPT, so a re-enable resumes with accumulated stats rather
        # than starting from zero.
        assert hooks.get_store() is not None

    @pytest.mark.asyncio
    async def test_startup_is_idempotent_and_does_not_build_a_second_runtime(
        self, tmp_path
    ):
        """Two runtimes would double-fire every reminder."""
        ctx = SimpleNamespace(data_dir=str(tmp_path))
        await hooks.on_startup(ctx)
        first = hooks.get_store()
        await hooks.on_startup(ctx)
        assert hooks.get_store() is first
        await hooks.on_shutdown(ctx)

    @pytest.mark.asyncio
    async def test_shutdown_before_startup_is_harmless(self, tmp_path):
        await hooks.on_shutdown(SimpleNamespace(data_dir=str(tmp_path)))

    @pytest.mark.asyncio
    async def test_a_reminder_added_before_disable_survives_re_enable(self, tmp_path):
        ctx = SimpleNamespace(data_dir=str(tmp_path))
        await hooks.on_startup(ctx)
        store = hooks.get_store()
        assert store is not None
        store.add("persisted", to_iso(NOW + timedelta(hours=3)))

        await hooks.on_shutdown(ctx)
        await hooks.on_startup(ctx)

        rows = hooks.get_store().snapshot()["reminders"]  # type: ignore[union-attr]
        assert [r["text"] for r in rows] == ["persisted"]
        await hooks.on_shutdown(ctx)
