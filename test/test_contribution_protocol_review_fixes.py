"""Contribution-protocol invariants at the boundaries it enforces.

Each case pins the mechanism its finding named: a quota spent by a write that
never landed, a retained-key count nothing bounded, a size-legal payload the
egress redactor could not survive, a teardown that reported a delete it had not
persisted, and unit resolution doing ledger I/O on the serving loop.

Harness mirrors ``test_contribution_protocol.py`` -- same fixtures, same routes.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import members
from kiro_crew.apps.manifest import AppManifest, Contributions
from kiro_crew.eventlog import contrib, grants
from kiro_crew.eventlog.contrib import (
    ContribError,
    ExternalProjectionStore,
    ProjectionDeleteIncomplete,
    set_store,
)
from kiro_crew.eventlog.service import get_service, set_service

CREW = "code-reviewer"
APP = "demoapp"
SLUG = "code-reviewer"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    contrib_root = tmp_path / "eventlog" / "contrib"
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    monkeypatch.setattr(contrib, "contrib_root", lambda: contrib_root)
    set_service(None)
    set_store(None)
    contrib.get_budget().reset()
    grants.invalidate()
    yield
    set_service(None)
    set_store(None)
    grants.invalidate()


def _grant(monkeypatch, *, app=APP, events=("demoapp/*",), projections=("demoapp/*",)):
    manifest = AppManifest(
        name=app,
        version="1.0.0",
        displayName=app,
        description="d",
        contributions=Contributions(
            events=list(events), projections=list(projections), units=["member"]
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest", lambda n: manifest if n == app else None
    )
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: n == app)
    grants.invalidate()
    return manifest


def _app(state, *, caller_app: str):
    from kiro_crew.dashboard.handlers.eventlog import (
        api_eventlog_events_get,
        api_eventlog_events_post,
        api_eventlog_projection_put,
    )

    @web.middleware
    async def _auth(request, handler):
        request["app"] = caller_app
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/eventlog/{kind}/{id}/events", api_eventlog_events_get)
    app.router.add_post("/api/eventlog/{kind}/{id}/events", api_eventlog_events_post)
    app.router.add_post("/api/eventlog/{kind}/{id}/projections/{key}", api_eventlog_projection_put)
    return app


def _ensure_log():
    get_service().ensure(SLUG, CREW)


# ---------------------------------------------------------------------------
# F8 -- a charge whose append failed is handed back
# ---------------------------------------------------------------------------
class TestQuotaIsNotSpentByAFailedWrite:
    @pytest.mark.asyncio
    async def test_a_failed_append_releases_its_charge(self, tmp_path, monkeypatch):
        """The finding: the reservation was taken before the write and never released.

        Ten failures then had to be paid for by ten honest appends that never
        happened, and at the limit a contributor is refused 429 for events the log
        does not hold.
        """
        _grant(monkeypatch)
        _ensure_log()
        budget = contrib.get_budget()

        def _boom(*a, **k):
            raise OSError("disk gone")

        monkeypatch.setattr(get_service(), "append", _boom)
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            for _ in range(3):
                res = await client.post(
                    f"/api/eventlog/member/{SLUG}/events",
                    json={"type": "demoapp/ping", "data": {"n": 1}},
                )
                assert res.status >= 400
        assert budget.used(APP, "member", SLUG) == 0

    @pytest.mark.asyncio
    async def test_a_successful_append_still_spends_its_charge(self, tmp_path, monkeypatch):
        """The release must not be a blanket refund: a committed event is paid for."""
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": "demoapp/ping", "data": {"n": 1}},
            )
            assert res.status == 201
        assert contrib.get_budget().used(APP, "member", SLUG) == 1

    def test_release_lands_on_the_day_that_was_charged(self):
        """A failure across UTC midnight must not credit a day it never spent."""
        budget = contrib.EventBudget()
        at = 1_600_000_000.0
        budget.charge(APP, "member", SLUG, now=at)
        next_day = at + 86_400
        budget.release(APP, "member", SLUG, now=next_day)
        assert budget.used(APP, "member", SLUG, now=at) == 1
        budget.release(APP, "member", SLUG, now=at)
        assert budget.used(APP, "member", SLUG, now=at) == 0

    def test_release_cannot_create_budget(self):
        budget = contrib.EventBudget()
        budget.release(APP, "member", SLUG)
        assert budget.used(APP, "member", SLUG) == 0


# ---------------------------------------------------------------------------
# F9 -- retained projection keys are counted, not only sized
# ---------------------------------------------------------------------------
class TestRetainedKeysAreBounded:
    def test_a_wildcard_contributor_cannot_retain_keys_without_end(self, tmp_path):
        store = ExternalProjectionStore(tmp_path / "rows")
        for i in range(contrib.MAX_PROJECTION_KEYS_PER_APP_UNIT):
            store.publish(
                "member", SLUG, f"{APP}/k{i}", app=APP, value={"i": i}, seq=i, state_version=0
            )
        with pytest.raises(ContribError) as exc:
            store.publish(
                "member", SLUG, f"{APP}/one-too-many", app=APP, value={}, seq=1, state_version=0
            )
        assert exc.value.code == "projection_limit"
        assert exc.value.status == 409
        assert len(store.values("member", SLUG)) == contrib.MAX_PROJECTION_KEYS_PER_APP_UNIT

    def test_the_cap_still_lets_a_held_key_be_republished(self, tmp_path):
        """The bound is on RETENTION, so updating a card must keep working at it."""
        store = ExternalProjectionStore(tmp_path / "rows")
        for i in range(contrib.MAX_PROJECTION_KEYS_PER_APP_UNIT):
            store.publish(
                "member", SLUG, f"{APP}/k{i}", app=APP, value={"i": i}, seq=i, state_version=0
            )
        store.publish(
            "member", SLUG, f"{APP}/k0", app=APP, value={"i": "new"}, seq=999, state_version=0
        )
        assert store.get("member", SLUG, f"{APP}/k0").value == {"i": "new"}

    def test_the_cap_is_per_app_so_one_contributor_cannot_crowd_out_another(self, tmp_path):
        store = ExternalProjectionStore(tmp_path / "rows")
        for i in range(contrib.MAX_PROJECTION_KEYS_PER_APP_UNIT):
            store.publish(
                "member", SLUG, f"{APP}/k{i}", app=APP, value={"i": i}, seq=i, state_version=0
            )
        store.publish("member", SLUG, "other/k0", app="other", value={}, seq=1, state_version=0)
        assert store.get("member", SLUG, "other/k0") is not None

    def test_a_schema_only_publish_is_counted_too(self, tmp_path):
        """Otherwise the schema route is a second, uncounted way to retain keys."""
        store = ExternalProjectionStore(tmp_path / "rows")
        for i in range(contrib.MAX_PROJECTION_KEYS_PER_APP_UNIT):
            store.publish(
                "member", SLUG, f"{APP}/k{i}", app=APP, value={"i": i}, seq=i, state_version=0
            )
        with pytest.raises(ContribError) as exc:
            store.put_schema("member", SLUG, f"{APP}/new", app=APP, schema={"kind": "badge"})
        assert exc.value.code == "projection_limit"


# ---------------------------------------------------------------------------
# F12 (door) -- an over-deep payload is refused at validation
# ---------------------------------------------------------------------------
class TestDepthIsRefusedAtTheDoor:
    def _deep(self, levels: int) -> dict:
        node: dict = {"leaf": 1}
        for _ in range(levels):
            node = {"n": node}
        return node

    def test_event_data_deeper_than_the_bound_is_refused(self):
        with pytest.raises(ContribError) as exc:
            contrib.check_event_data(self._deep(contrib.MAX_VALUE_DEPTH + 5))
        assert exc.value.code == "invalid_projection_value"

    def test_a_projection_value_deeper_than_the_bound_is_refused(self):
        with pytest.raises(ContribError) as exc:
            contrib.check_projection_value(self._deep(contrib.MAX_VALUE_DEPTH + 5))
        assert exc.value.code == "invalid_projection_value"

    def test_a_payload_inside_the_bound_is_accepted(self):
        contrib.check_event_data(self._deep(contrib.MAX_VALUE_DEPTH - 2))
        contrib.check_projection_value(self._deep(contrib.MAX_VALUE_DEPTH - 2))

    def test_the_depth_check_does_not_itself_recurse(self):
        """A recursive CHECK would raise the very error it exists to prevent."""
        with pytest.raises(ContribError):
            contrib.check_event_data(self._deep(50_000))

    @pytest.mark.asyncio
    async def test_the_append_route_answers_400_for_a_deep_payload(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": "demoapp/ping", "data": self._deep(contrib.MAX_VALUE_DEPTH + 5)},
            )
            status, body = res.status, await res.json()
        assert status == 400 and body["code"] == "invalid_projection_value"
        assert get_service().last_seq(SLUG) == -1


# ---------------------------------------------------------------------------
# F4 -- teardown does not report a delete it could not persist
# ---------------------------------------------------------------------------
class TestTeardownDeletionIsDurable:
    def test_an_unwritable_unit_is_reported_and_its_rows_are_kept(self, tmp_path, monkeypatch):
        """The old flush was best-effort: the rows came back on the next cold load
        while the dashboard had already been told the card was gone.

        A SECOND app's row is published so the rewrite path is the one under test
        -- with nothing left the file is unlinked instead, which is a different
        failure to provoke.
        """
        store = ExternalProjectionStore(tmp_path / "rows")
        store.publish(
            "member", SLUG, f"{APP}/card", app=APP, value={"a": 1}, seq=1, state_version=0
        )
        store.publish(
            "member", SLUG, "other/card", app="other", value={"b": 2}, seq=1, state_version=0
        )

        def _fail(*a, **k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr("kiro_crew.atomic_write.atomic_write", _fail)
        with pytest.raises(ProjectionDeleteIncomplete) as exc:
            store.delete_app_rows(APP)
        assert exc.value.removed == []
        assert exc.value.failed == [f"member/{SLUG}"]
        # Still held in memory, because it is still on disk.
        assert store.get("member", SLUG, f"{APP}/card") is not None
        assert store.get("member", SLUG, "other/card") is not None

    def test_a_successful_teardown_still_returns_what_it_removed(self, tmp_path):
        store = ExternalProjectionStore(tmp_path / "rows")
        store.publish(
            "member", SLUG, f"{APP}/card", app=APP, value={"a": 1}, seq=1, state_version=0
        )
        removed = store.delete_app_rows(APP)
        assert removed == [("member", SLUG, f"{APP}/card")]
        assert store.get("member", SLUG, f"{APP}/card") is None

    def test_flush_has_no_swallowing_mode_left(self):
        """A `best_effort` switch is how the undurable delete was spelled."""
        import inspect

        params = inspect.signature(ExternalProjectionStore._flush).parameters
        assert "best_effort" not in params


# ---------------------------------------------------------------------------
# F11 -- unit resolution never runs its ledger read on the serving loop
# ---------------------------------------------------------------------------
class TestUnitResolutionIsOffloaded:
    @pytest.mark.asyncio
    async def test_every_eventlog_handler_resolves_the_unit_off_the_loop(
        self, tmp_path, monkeypatch
    ):
        """`resolve_unit` calls `last_seq`, which loads and folds a cold ledger.

        Asserted by identity of the running thread inside the call, not by reading
        the source: a future refactor that moves it back onto the loop fails here.
        """
        import threading

        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        _grant(monkeypatch)
        _ensure_log()
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = handlers_mod.resolve_unit

        def _record(kind, unit_id):
            seen.append(threading.get_ident())
            return real(kind, unit_id)

        monkeypatch.setattr(handlers_mod, "resolve_unit", _record)
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get(f"/api/eventlog/member/{SLUG}/events")).status == 200
            assert (
                await client.post(
                    f"/api/eventlog/member/{SLUG}/events",
                    json={"type": "demoapp/ping", "data": {}},
                )
            ).status == 201
            assert (
                await client.post(
                    f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcard",
                    json={"value": {"a": 1}, "seq": 1},
                )
            ).status == 204
        assert len(seen) == 3
        assert all(t != loop_thread for t in seen), "resolve_unit ran on the serving loop"


def test_the_serving_loop_is_the_thread_the_test_above_compares_against():
    """Guards the test above from passing because BOTH ran off-loop."""

    async def _main() -> int:
        import threading

        return threading.get_ident()

    import threading

    assert asyncio.run(_main()) == threading.get_ident()
