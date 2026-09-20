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
        before = get_service().last_seq(SLUG)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": "demoapp/ping", "data": self._deep(contrib.MAX_VALUE_DEPTH + 5)},
            )
            status, body = res.status, await res.json()
        assert status == 400 and body["code"] == "invalid_projection_value"
        # Refused at the door, so the seq does not move. Asserted as a delta rather
        # than as an empty log: emptiness also fails when the gateway holds an event
        # of its own, and this test is named for the refusal.
        assert get_service().last_seq(SLUG) == before


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


# ---------------------------------------------------------------------------
# G6 -- a retained schema selector is length-bounded, not just count-bounded
# ---------------------------------------------------------------------------
class TestSchemaSelectorsAreLengthBounded:
    """The store retains up to 64 schemas, so an unbounded selector is retained
    64 x 32 times. Bounding only the COUNT left the per-selector string free to
    grow, which a granted app can use to grow disk and memory without limit."""

    def test_an_oversized_selector_is_truncated(self):
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "path": ["x" * 5000]})
        assert len(out["path"][0]) == 120, "the selector kept its unbounded length"

    def test_an_ordinary_selector_is_untouched(self):
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "path": ["status", "lastRun"]})
        assert out["path"] == ["status", "lastRun"]

    def test_the_count_bound_still_holds(self):
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "path": [f"s{i}" for i in range(200)]})
        assert len(out["path"]) == 32

    def test_the_whole_retained_schema_is_bounded(self):
        """Both bounds together are what cap one stored schema's size."""
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema(
            {"kind": "keyvalue", "title": "t" * 9000, "path": ["p" * 9000] * 200}
        )
        assert len(out["title"]) == 120
        assert len(out["path"]) == 32
        assert all(len(sel) == 120 for sel in out["path"])


# ---------------------------------------------------------------------------
# H1 -- the projection egress redaction fails CLOSED
# ---------------------------------------------------------------------------
class TestProjectionEgressFailsClosed:
    """A redactor fault must not publish the value the redactor exists to scrub.

    Every other call site of the shared redactor is already fail-closed: the
    catch-up read, the roster seed and the two member HTTP reads let the
    exception reach the handler, and the live event fan-out drops the frame and
    closes its subscribers. This broadcast is the same class of egress.
    """

    def _service(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as service_mod

        svc = service_mod.MemberEventLogService(tmp_path)
        sent: list[tuple] = []
        svc._broadcast = lambda frame, payload: sent.append((frame, payload))
        return svc, sent, service_mod

    def test_a_redactor_fault_drops_the_frame(self, tmp_path, monkeypatch):
        svc, sent, service_mod = self._service(tmp_path, monkeypatch)

        def _boom(_value, _depth=1):
            raise RuntimeError("redactor exploded")

        monkeypatch.setattr(service_mod, "_redact_projection_value", _boom)
        svc._on_change("alice", "wake", {"token": "AKIAIOSFODNN7EXAMPLE"}, 3)
        assert sent == [], "the unredacted view was broadcast on a redactor fault"

    def test_an_ordinary_fold_still_broadcasts(self, tmp_path, monkeypatch):
        svc, sent, _mod = self._service(tmp_path, monkeypatch)
        svc._on_change("alice", "wake", {"patrol": "armed"}, 3)
        assert len(sent) == 1, "the gate must only close on a fault"


# ---------------------------------------------------------------------------
# H2 -- a projection row is durable before anything publishes it
# ---------------------------------------------------------------------------
class TestProjectionWritesAreDurable:
    """The response and the broadcast both follow ``_flush``, so its write must
    be on the platter before it returns -- including the DELETE, whose durability
    lives in the parent directory entry rather than in any file."""

    def test_a_written_row_syncs_the_file_and_its_directory(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import contrib as contrib_mod

        calls: dict[str, object] = {}
        real_atomic = None

        from pathlib import Path

        def _atomic(path, payload, **kw):
            calls["fsync"] = kw.get("fsync")
            Path(path).write_text(payload, encoding="utf-8")

        def _fsync_dir(path, **kw):
            calls["dir"] = str(path)

        import kiro_crew.atomic_write as aw_mod

        monkeypatch.setattr(aw_mod, "atomic_write", _atomic)
        monkeypatch.setattr(aw_mod, "fsync_dir", _fsync_dir)
        store = contrib_mod.ExternalProjectionStore(tmp_path)
        row = contrib_mod.ExternalRow(value={"a": 1}, seq=1, state_version=0, app="demoapp")
        store._flush("member", "alice", {"demoapp/k": row})
        assert calls.get("fsync") is True, "the file write did not fsync"
        assert "dir" in calls, "the parent directory entry was never synced"
        assert real_atomic is None

    def test_a_delete_syncs_the_directory_that_held_the_entry(self, tmp_path, monkeypatch):
        import kiro_crew.atomic_write as aw_mod
        from kiro_crew.eventlog import contrib as contrib_mod

        synced: list[str] = []
        monkeypatch.setattr(aw_mod, "fsync_dir", lambda path, **kw: synced.append(str(path)))
        store = contrib_mod.ExternalProjectionStore(tmp_path)
        path = store._path("member", "alice")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        store._flush("member", "alice", {})
        assert not path.exists()
        assert synced, "an unlinked row can come back without a directory sync"

    def test_deleting_what_was_never_there_syncs_nothing(self, tmp_path, monkeypatch):
        import kiro_crew.atomic_write as aw_mod
        from kiro_crew.eventlog import contrib as contrib_mod

        synced: list[str] = []
        monkeypatch.setattr(aw_mod, "fsync_dir", lambda path, **kw: synced.append(str(path)))
        store = contrib_mod.ExternalProjectionStore(tmp_path)
        store._flush("member", "nobody", {})
        assert synced == [], "no entry was removed, so there is nothing to sync"


# ---------------------------------------------------------------------------
# H3 -- an unreadable projection root is reported, not silently partial
# ---------------------------------------------------------------------------
class TestEnumerationFailureIsPropagated:
    """The list decides what teardown deletes, so a partial answer returned as
    whole has teardown report success over units it never saw."""

    def test_an_unreadable_root_raises(self, tmp_path, monkeypatch):
        from pathlib import Path

        from kiro_crew.eventlog import contrib as contrib_mod

        store = contrib_mod.ExternalProjectionStore(tmp_path / "root")
        (tmp_path / "root").mkdir()

        def _boom(self):
            raise OSError("EIO")

        monkeypatch.setattr(Path, "iterdir", _boom)
        with pytest.raises(contrib_mod.ContribError) as got:
            store._known_units()
        assert got.value.code == "projection_root_unreadable"

    def test_a_missing_root_is_still_just_no_units(self, tmp_path):
        from kiro_crew.eventlog import contrib as contrib_mod

        store = contrib_mod.ExternalProjectionStore(tmp_path / "never-created")
        assert store._known_units() == []


# ---------------------------------------------------------------------------
# H4 -- a deferred retraction must not strip a same-name REPLACEMENT
# ---------------------------------------------------------------------------
class TestDeferredRetractionRespectsAReplacement:
    """``forget_app_hooks`` is sync, so it schedules the retraction and returns --
    which is also when its caller releases the app lifecycle lock. A same-name
    install blocked on that lock can therefore be live before the retraction runs,
    and retracting then revokes the NEW app's grant and deletes the rows it has
    just published. Taking the lock is not enough on its own, because the install
    may simply win it first; the app's presence has to be re-checked under it."""

    def _lock(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def _noop(_name):
            yield

        return _noop

    @pytest.mark.asyncio
    async def test_a_replacement_is_left_alone(self, monkeypatch):
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps import teardown as teardown_mod

        retracted: list[str] = []
        monkeypatch.setattr(manager_mod, "app_lifecycle_lock", self._lock())
        monkeypatch.setattr(manager_mod, "get_app_manifest", lambda n: object())
        monkeypatch.setattr(
            teardown_mod,
            "teardown_contributions",
            lambda name: retracted.append(name) or _done(),
        )
        await teardown_mod._retract_contributions_if_still_gone("demoapp")
        assert retracted == [], "the replacement's grant and rows were retracted"

    @pytest.mark.asyncio
    async def test_an_app_that_is_still_gone_is_retracted(self, monkeypatch):
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps import teardown as teardown_mod

        retracted: list[str] = []
        monkeypatch.setattr(manager_mod, "app_lifecycle_lock", self._lock())
        monkeypatch.setattr(manager_mod, "get_app_manifest", lambda n: None)
        monkeypatch.setattr(
            teardown_mod,
            "teardown_contributions",
            lambda name: retracted.append(name) or _done(),
        )
        await teardown_mod._retract_contributions_if_still_gone("demoapp")
        assert retracted == ["demoapp"], "a genuinely removed app was not retracted"

    @pytest.mark.asyncio
    async def test_a_failure_is_logged_and_never_raised(self, monkeypatch):
        """It runs detached, so raising would surface nowhere and kill the task."""
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps import teardown as teardown_mod

        monkeypatch.setattr(manager_mod, "app_lifecycle_lock", self._lock())

        def _boom(_n):
            raise RuntimeError("manifest read failed")

        monkeypatch.setattr(manager_mod, "get_app_manifest", _boom)
        await teardown_mod._retract_contributions_if_still_gone("demoapp")


async def _done() -> list[str]:
    """An awaitable the retraction stubs can return."""
    return []


# ---------------------------------------------------------------------------
# The CLASS, not the five points: ratchets over every sibling entry point
# ---------------------------------------------------------------------------
class TestEveryRedactorEgressSiteFailsClosed:
    """One fail-open site was found by review; the value of fixing it is lost if
    the next egress path added reintroduces it. Every call site of the shared
    redactor either lets the exception propagate (the HTTP reads: the handler
    answers 5xx and nothing crosses) or drops the frame explicitly (the two live
    fan-outs). What none of them may do is substitute the unredacted value."""

    _MODULES = (
        "kiro_crew.dashboard.handlers.eventlog",
        "kiro_crew.dashboard.handlers.members",
        "kiro_crew.dashboard.eventlog_ws",
        "kiro_crew.eventlog.service",
    )

    def test_no_site_substitutes_the_unredacted_value(self):
        """Checked on the AST, not on nearby lines.

        A line-window regex misses the shape entirely once an explanatory comment
        sits between the ``except`` and the assignment -- which is exactly where
        such a comment belongs, so the check has to be insensitive to distance.
        """
        import ast
        import importlib
        import inspect

        raw_names = {"view", "value", "data", "event", "block", "schema"}
        offenders: list[str] = []
        for name in self._MODULES:
            mod = importlib.import_module(name)
            src = inspect.getsource(mod)
            if "_redact_projection_value" not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                for inner in ast.walk(node):
                    if not isinstance(inner, (ast.Assign, ast.AnnAssign)):
                        continue
                    val = inner.value
                    if isinstance(val, ast.Name) and val.id in raw_names:
                        line = inner.lineno
                        offenders.append(f"{name}:{line} assigns raw {val.id!r}")
        assert offenders == [], f"a redactor egress site falls back to the raw value: {offenders}"

    def test_every_module_that_redacts_is_in_this_ratchet(self):
        """An egress path outside the list is one this test cannot speak for."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        using = {
            str(p.relative_to(root)).replace("/", ".").replace("\\", ".")[:-3]
            for p in root.rglob("*.py")
            if "_redact_projection_value(" in p.read_text(encoding="utf-8")
        }
        # The redactor's own module defines and recurses into it; posture modules
        # only name it in prose.
        using -= {"eventlog.types", "security_posture"}
        covered = {m.removeprefix("kiro_crew.") for m in self._MODULES}
        assert using <= covered, f"unratcheted egress module(s): {sorted(using - covered)}"


class TestTheStoreHasOneWritePath:
    """``_flush`` is where publish AND teardown both land, which is what makes
    one durability bound cover the store. A second write path would be outside
    that bound and would publish before its bytes were durable."""

    def test_flush_is_the_only_writer(self):
        import inspect

        from kiro_crew.eventlog import contrib as contrib_mod

        src = inspect.getsource(contrib_mod)
        writes = [
            ln.strip()
            for ln in src.splitlines()
            if ("atomic_write(" in ln or ".write_text(" in ln or ".unlink(" in ln)
            and not ln.strip().startswith(("#", "*", '"', "'"))
            and "import" not in ln
        ]
        assert len(writes) == 2, f"expected the atomic_write and the unlink, got {writes}"
        assert any("fsync=True" in w for w in writes), "the surviving write does not fsync"


# ---------------------------------------------------------------------------
# I1 -- a WebSocket subscription decision is audited like its HTTP siblings
# ---------------------------------------------------------------------------
class TestSubscriptionDecisionsAreAudited:
    """A subscription is a contribution decision, so it belongs in the same SEL
    stream as the reads and appends. One that leaves no event is one no audit can
    account for, and the refusal matters as much as the grant."""

    def test_the_shim_delegates_to_the_http_contribution_audit(self, monkeypatch):
        from kiro_crew.dashboard import ws as ws_mod
        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        seen: list[tuple] = []
        monkeypatch.setattr(
            handlers_mod,
            "_audit",
            lambda app, op, outcome, res, error="": seen.append((app, op, outcome, res, error)),
        )
        ws_mod._audit_contribution("demoapp", "eventlog.subscribe", "denied", "member/x", "nope")
        assert seen == [("demoapp", "eventlog.subscribe", "denied", "member/x", "nope")]

    def test_an_audit_fault_never_changes_the_answer(self, monkeypatch):
        from kiro_crew.dashboard import ws as ws_mod
        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        def _boom(*_a, **_kw):
            raise RuntimeError("SEL unavailable")

        monkeypatch.setattr(handlers_mod, "_audit", _boom)
        ws_mod._audit_contribution("demoapp", "eventlog.subscribe", "granted", "member/x")

    def test_both_outcomes_are_recorded_by_the_frame_handler(self):
        """Source ratchet: the refusal path and the granted path each audit."""
        import inspect

        from kiro_crew.dashboard import ws as ws_mod

        src = inspect.getsource(ws_mod)
        assert '_audit_contribution(ws_app, "eventlog.subscribe", "denied"' in src
        assert '_audit_contribution(ws_app, "eventlog.subscribe", "granted"' in src


# ---------------------------------------------------------------------------
# I2 + the class -- a retained field bounded in COUNT is bounded in LENGTH too
# ---------------------------------------------------------------------------
class TestRetainedFieldsAreBoundedBothWays:
    """A count bound alone lets the maximum number of unbounded strings through,
    which is the same amount of memory with extra steps. Both retained fields this
    change introduces are bounded on each axis."""

    def _manifest(self, patterns):
        from kiro_crew.apps.manifest import AppManifest, Contributions

        return AppManifest(
            name="demoapp",
            version="1.0.0",
            displayName="demoapp",
            description="d",
            contributions=Contributions(events=list(patterns), projections=[]),
        )

    def test_an_overlong_contribution_pattern_is_rejected(self, tmp_path):
        errors = self._manifest(["demoapp/" + "x" * 5000]).validate(tmp_path)
        assert any("over the limit" in e for e in errors), errors

    def test_an_ordinary_pattern_is_accepted(self, tmp_path):
        errors = self._manifest(["demoapp/*"]).validate(tmp_path)
        assert not any("over the limit" in e for e in errors), errors

    def test_the_schema_selector_sibling_is_bounded_the_same_way(self):
        """The other retained field in this change, bounded on both axes."""
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "path": ["y" * 4000] * 90})
        assert len(out["path"]) == 32
        assert all(len(sel) == 120 for sel in out["path"])


# ---------------------------------------------------------------------------
# I3, I4, I5, I7 -- the import and migration boundaries
# ---------------------------------------------------------------------------
class TestImportAndMigrationBoundaries:
    def test_a_reparse_point_is_refused_like_a_symlink(self, tmp_path):
        """``is_symlink()`` alone is not the boundary: a junction is a reparse
        point it does not report, and descending one leaves the package root."""
        from kiro_crew.apps import plugin_import as pi

        target = tmp_path / "outside.txt"
        target.write_text("SECRET", encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(target)
        assert pi._is_link_or_reparse(link) is True

    def test_an_unstattable_entry_is_refused(self, tmp_path):
        """An entry that cannot be judged is not one to descend into."""
        from kiro_crew.apps import plugin_import as pi

        assert pi._is_link_or_reparse(tmp_path / "does-not-exist") is True

    def test_a_plain_file_is_not_refused(self, tmp_path):
        from kiro_crew.apps import plugin_import as pi

        ordinary = tmp_path / "f.txt"
        ordinary.write_text("fine", encoding="utf-8")
        assert pi._is_link_or_reparse(ordinary) is False

    def test_a_reparse_attribute_alone_is_enough_to_refuse(self):
        """The junction case, and the only one that needs the attribute check.

        A junction is not a symlink and is stattable, so the symlink and
        unstattable tests both pass without this branch existing at all -- which is
        what makes it worth pinning on its own. POSIX cannot create one, and
        faking it by patching ``os.lstat`` patches it for the temporary-directory
        teardown too, so the attribute is handed to the helper directly.
        """
        from kiro_crew.apps.plugin_import import _has_reparse_attribute

        class _St:
            st_file_attributes = 0x400

        assert _has_reparse_attribute(_St()) is True

    def test_a_zero_attribute_stat_is_not_refused(self):
        """POSIX stat carries no such attribute, so absence must read as 'plain'."""
        from kiro_crew.apps.plugin_import import _has_reparse_attribute

        class _Zero:
            st_file_attributes = 0

        class _Absent:
            pass

        assert _has_reparse_attribute(_Zero()) is False
        assert _has_reparse_attribute(_Absent()) is False

    def test_an_oversized_manifest_is_refused_before_it_is_read(self, tmp_path, monkeypatch):
        from kiro_crew.apps import plugin_import as pi

        big = tmp_path / "plugin.json"
        big.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(pi, "MAX_MANIFEST_BYTES", 1)
        reads: list[str] = []
        real_read = pi.Path.read_text
        monkeypatch.setattr(
            pi.Path,
            "read_text",
            lambda self, *a, **kw: (reads.append(str(self)), real_read(self, *a, **kw))[1],
        )
        with pytest.raises(pi.PluginImportError) as got:
            pi._read_json_object(big, "plugin manifest")
        assert got.value.code == "manifest_too_large"
        assert reads == [], "the file was read despite being over the ceiling"

    def test_an_output_path_that_is_a_file_is_refused_with_a_code(self, tmp_path):
        """Not a stray NotADirectoryError out of the emptiness probe."""
        from kiro_crew.apps import plugin_import as pi

        out = tmp_path / "out.txt"
        out.write_text("in the way", encoding="utf-8")
        with pytest.raises(pi.PluginImportError) as got:
            pi.convert_plugin_package(tmp_path / "src", out)
        assert got.value.code == "output_not_a_directory"

    def test_an_oversized_legacy_activity_file_is_skipped_not_read(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as svc_mod

        monkeypatch.setattr(svc_mod, "_MAX_LEGACY_ACTIVITY_BYTES", 4)
        assert svc_mod._MAX_LEGACY_ACTIVITY_BYTES == 4
        import inspect

        reader = inspect.getsource(svc_mod._read_legacy_activity_files)
        assert "_MAX_LEGACY_ACTIVITY_BYTES" in reader

        # A path-based whole-file read is the construct that cannot be bounded:
        # it decides nothing about the object it loads and has already spent the
        # memory by the time any ceiling is consulted. The legacy reader must
        # hold none, and must measure and read ONE descriptor instead.
        assert "read_text" not in reader, "the legacy read went back to a path-based whole read"
        assert "os.fstat(fd)" in reader, "the size is not measured on the descriptor that is read"
        i_size = reader.index("st.st_size > _MAX_LEGACY_ACTIVITY_BYTES")
        i_read = reader.index("fh.read(")
        assert i_size < i_read, "the ceiling is consulted after the read, not before"


# ---------------------------------------------------------------------------
# I6 (bound half) -- one unit's log has a cumulative ceiling
# ---------------------------------------------------------------------------
class TestUnitLogHasACumulativeCeiling:
    """The per-append size check bounds one event and the daily quota bounds one
    day, but a quota RENEWS, so neither bounds the total a contributor can
    accumulate in one log. The fold materializes that total on a cold load, so the
    total is what decides the fold's cost."""

    def _log(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import log as log_mod

        monkeypatch.setattr("kiro_crew.crew_log.store.crew_log_tree_root", lambda: tmp_path)
        lg = log_mod.MemberLog("alice")
        lg.create("alice")
        return log_mod, lg

    def test_an_append_over_the_ceiling_is_refused(self, tmp_path, monkeypatch):
        log_mod, lg = self._log(tmp_path, monkeypatch)
        lg.append("member/message", {"ts": 1, "preview": "one"})
        monkeypatch.setattr(log_mod, "MAX_UNIT_LOG_BYTES", 1)
        with pytest.raises(log_mod.UnitLogFull) as got:
            lg.append("member/message", {"ts": 2, "preview": "two"})
        assert got.value.limit == 1
        assert got.value.size > 1

    def test_the_refusal_is_its_own_type_not_a_value_error(self, tmp_path, monkeypatch):
        """'No room left' is answered by pruning; 'malformed' by fixing the call."""
        log_mod, lg = self._log(tmp_path, monkeypatch)
        assert not issubclass(log_mod.UnitLogFull, ValueError)

    def test_an_ordinary_append_is_untouched(self, tmp_path, monkeypatch):
        log_mod, lg = self._log(tmp_path, monkeypatch)
        ev = lg.append("member/message", {"ts": 1, "preview": "fine"})
        assert ev is not None


class TestRevocationGenerationFencesEveryCommit:
    """J3: a mutation authorized before a suspension must not commit after a revoke.

    The window is real on all four mutating paths: each checks its grant, then
    offloads ``resolve_unit`` (which walks the store and folds a ledger for an
    untouched unit), and a disable landing in between revokes the grant and
    deletes the app's rows synchronously. Resuming and writing puts a torn-down
    app's state back.
    """

    def _fresh_store(self, tmp_path):
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        return ExternalProjectionStore(tmp_path / "contrib")

    def test_the_generation_moves_on_every_grant_state_change(self):
        from kiro_crew.eventlog import grants

        start = grants.revocation_generation()
        grants.revoke("fence-probe")
        after_revoke = grants.revocation_generation()
        grants.unrevoke("fence-probe")
        after_unrevoke = grants.revocation_generation()
        grants.invalidate("fence-probe")
        after_invalidate = grants.revocation_generation()

        # Each is a distinct step, so a mutation that read any earlier value
        # sees a change. Asserting only "it moved once" would pass with two of
        # the three bumps deleted.
        assert start < after_revoke < after_unrevoke < after_invalidate

    def test_an_unfenced_caller_is_permitted(self):
        """``None`` is additive, so a host-side write with no suspension is unchanged."""
        from kiro_crew.eventlog.contrib import assert_grants_unchanged

        assert assert_grants_unchanged(None) is None

    def test_an_unchanged_generation_passes(self):
        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import assert_grants_unchanged

        assert assert_grants_unchanged(grants.revocation_generation()) is None

    def test_a_moved_generation_is_refused_with_its_own_code(self):
        import pytest

        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ContribError, assert_grants_unchanged

        fence = grants.revocation_generation()
        grants.revoke("fence-refused")
        try:
            with pytest.raises(ContribError) as caught:
                assert_grants_unchanged(fence)
        finally:
            grants.unrevoke("fence-refused")

        assert caught.value.code == "app_revoked"
        # 409 not 403: the counter moves for any app's lifecycle event, so the
        # fence cannot claim THIS app lost authority, only that the world moved.
        assert caught.value.status == 409

    def test_publish_refuses_a_stale_fence_and_writes_nothing(self, tmp_path):
        import pytest

        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ContribError

        store = self._fresh_store(tmp_path)
        fence = grants.revocation_generation()
        grants.revoke("pub-app")
        try:
            with pytest.raises(ContribError) as caught:
                store.publish(
                    "member",
                    "someone",
                    "pub-app/card",
                    app="pub-app",
                    value={"a": 1},
                    seq=1,
                    state_version=0,
                    expect_generation=fence,
                )
        finally:
            grants.unrevoke("pub-app")

        assert caught.value.code == "app_revoked"
        # The refusal is what matters, but so is its completeness: a fence that
        # raised AFTER the row was written would recreate exactly the state the
        # teardown deleted.
        assert store.get("member", "someone", "pub-app/card") is None

    def test_put_schema_refuses_a_stale_fence_and_creates_no_row(self, tmp_path):
        import pytest

        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ContribError

        store = self._fresh_store(tmp_path)
        fence = grants.revocation_generation()
        grants.revoke("schema-app")
        try:
            with pytest.raises(ContribError) as caught:
                store.put_schema(
                    "member",
                    "someone",
                    "schema-app/card",
                    app="schema-app",
                    schema={"kind": "text"},
                    expect_generation=fence,
                )
        finally:
            grants.unrevoke("schema-app")

        assert caught.value.code == "app_revoked"
        # put_schema CREATES a value-less row, so leaving it unfenced would have
        # been a second way to put a torn-down app's state back.
        assert store.get("member", "someone", "schema-app/card") is None

    def test_a_current_fence_still_commits(self, tmp_path):
        """The complement: the fence refuses a stale generation, not every write."""
        from kiro_crew.eventlog import grants

        store = self._fresh_store(tmp_path)
        store.publish(
            "member",
            "someone",
            "live-app/card",
            app="live-app",
            value={"a": 1},
            seq=1,
            state_version=0,
            expect_generation=grants.revocation_generation(),
        )

        row = store.get("member", "someone", "live-app/card")
        assert row is not None
        assert row.value == {"a": 1}

    def test_every_mutating_commit_path_reads_the_fence(self):
        """Enumeration guard: four commits, four fences, checked by source.

        Point-fixing one cited line and calling the class closed is how the
        previous rounds' siblings were missed. This fails when a fifth mutating
        path is added without a fence, rather than waiting for a reviewer to
        find it.
        """
        import pathlib

        import kiro_crew.dashboard.handlers.eventlog as http_handlers
        import kiro_crew.dashboard.ws as ws_mod
        import kiro_crew.eventlog.contrib as contrib_mod

        store_src = pathlib.Path(contrib_mod.__file__).read_text(encoding="utf-8")
        # Both store-side commits assert INSIDE their per-unit lock.
        assert store_src.count("assert_grants_unchanged(expect_generation)") == 2

        http_src = pathlib.Path(http_handlers.__file__).read_text(encoding="utf-8")
        # Three mutating HTTP handlers: append, publish, schema. Each reads the
        # generation before its grant check.
        assert http_src.count("fence = grants.revocation_generation()") == 3
        assert http_src.count("expect_generation=fence") == 2
        assert "assert_grants_unchanged(fence)" in http_src

        ws_src = pathlib.Path(ws_mod.__file__).read_text(encoding="utf-8")
        assert ws_src.count("fence = grants.revocation_generation()") == 1
        assert "assert_grants_unchanged(fence)" in ws_src


class TestScopeManifestReadsAreOffLoop:
    """J4: authorization must not read an app manifest on the serving loop.

    ``permissions.api`` and the contributions declaration both come from the
    app's manifest, which has no cache of its own. The old caches expired on a
    30-second timer, so the first request after each lapse did file I/O inside
    the auth middleware. Two changes together: the caches are keyed on the grant
    generation, so only a lifecycle event invalidates them, and the middleware
    resolves both in an executor before the sync decision asks.
    """

    def test_both_caches_are_keyed_on_the_same_generation(self):
        """One counter for both, so permissions and contributions cannot drift."""
        from kiro_crew.dashboard import token_auth
        from kiro_crew.eventlog import grants

        assert token_auth._scope_generation() == grants.revocation_generation()

        grants.invalidate("scope-gen-probe")
        assert token_auth._scope_generation() == grants.revocation_generation()

    def test_a_lifecycle_event_invalidates_the_permissions_cache(self):
        from kiro_crew.dashboard import token_auth
        from kiro_crew.eventlog import grants

        app = "scope-cache-probe"
        generation = token_auth._scope_generation()
        assert generation is not None
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache[app] = (generation, ("/api/probe",))

        # Warm: served from the cache without touching a manifest.
        assert token_auth._app_api_allowlist(app) == ("/api/probe",)

        grants.revoke(app)
        try:
            # The bump alone makes the entry stale, so the planted value is gone
            # and the deny-safe read (no such app installed) replaces it.
            assert token_auth._app_api_allowlist(app) == ()
        finally:
            grants.unrevoke(app)
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache.pop(app, None)

    def test_a_lifecycle_event_invalidates_the_contributions_cache(self):
        """The declaration cache must honour the counter, not merely exist.

        Added because a mutation reverting the generation check to "any cached
        entry wins" left every other test in this class green: they pin the
        token_auth cache and the counter itself, and neither observes whether
        the declaration cache reads the counter at all.
        """
        from kiro_crew.eventlog import grants

        app = "decl-cache-probe"
        generation = grants.revocation_generation()
        with grants._cache_lock:
            grants._cache[app] = (generation, ((), (), ("member",)))
        try:
            assert grants.may_use_kind(app, "member") is True

            # A lifecycle event on ANOTHER app still bumps the shared counter, so
            # the planted entry goes stale and the deny-safe read replaces it.
            # Naming a different app also proves the invalidation is the COUNTER
            # rather than this entry being popped by name.
            grants.invalidate("some-other-app")
            assert grants.may_use_kind(app, "member") is False
        finally:
            with grants._cache_lock:
                grants._cache.pop(app, None)

    def test_coldness_is_answered_without_touching_the_filesystem(self):
        """The middleware's gate must be loop-safe, or it is the bug it prevents."""
        from kiro_crew.dashboard import token_auth

        app = "scope-cold-probe"
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache.pop(app, None)
        assert token_auth._app_scope_is_cold(app) is True

    def test_warming_makes_the_app_warm_and_is_idempotent(self):
        import asyncio

        from kiro_crew.dashboard import token_auth

        app = "scope-warm-probe"
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache.pop(app, None)
        try:
            asyncio.run(token_auth.warm_app_scope(app))
            assert token_auth._app_scope_is_cold(app) is False
            # Second call is a no-op rather than a second manifest walk.
            asyncio.run(token_auth.warm_app_scope(app))
            assert token_auth._app_scope_is_cold(app) is False
        finally:
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache.pop(app, None)

    def test_an_empty_app_name_warms_nothing(self):
        """A dashboard-user token has no manifest, so it must not pay a hop."""
        import asyncio

        from kiro_crew.dashboard import token_auth

        asyncio.run(token_auth.warm_app_scope(""))

    def test_the_scope_check_is_async_and_every_call_site_awaits_it(self):
        """Enumeration guard: a fourth call site added un-awaited fails here.

        An un-awaited coroutine is falsy-but-not-None, so the middleware would
        read it as "no denial" and admit every out-of-scope request while
        emitting only a RuntimeWarning. That is a silent authorization bypass,
        which is why this is pinned by count rather than by review.
        """
        import inspect
        import pathlib

        from kiro_crew.dashboard import token_auth

        assert inspect.iscoroutinefunction(token_auth._enforce_app_scope)

        src = pathlib.Path(token_auth.__file__).read_text(encoding="utf-8")
        # Exclude the definition rather than counting a bare substring: black
        # decides whether the signature fits on one line, so a pattern that
        # happens to match it today silently counts the def as a call tomorrow.
        mentions = [ln for ln in src.splitlines() if "_enforce_app_scope(" in ln]
        calls = [ln for ln in mentions if not ln.lstrip().startswith(("def ", "async def "))]
        assert len(calls) == 3, f"expected 3 call sites, found {len(calls)}"
        assert all(
            "await _enforce_app_scope(" in ln for ln in calls
        ), "every call site must be awaited"

    def test_the_warm_runs_before_the_decision_reads_the_manifest(self):
        """Order is the property: warming after the check would warm nothing."""
        import inspect

        from kiro_crew.dashboard import token_auth

        body = inspect.getsource(token_auth._enforce_app_scope)
        assert body.index("await warm_app_scope(") < body.index("app_token_path_allowed(")


class TestUnboundedReadsAndWalksAreBounded:
    """Every manifest read and every discovery walk carries its own ceiling.

    A sweep scoped to one module misses a sibling copy of the same read in
    another, so this class pins all three members together: the CLI's name
    derivation, the skill-discovery walk, and the projection loader. The last
    test enumerates the call sites rather than trusting that they agree.
    """

    def test_the_cli_derives_the_app_name_through_the_bounded_reader(self, tmp_path):
        """A vendor manifest whose top level is a list must refuse, not crash.

        The name derivation calls `.get` on whatever the manifest parses to, so a
        bare `[]` must be refused by the reader rather than reaching that call and
        raising AttributeError out of a command. The same reader carries the size
        ceiling, so routing through it supplies both.
        """
        import pytest

        from kiro_crew.apps.plugin_import import PluginImportError, read_manifest_name

        listed = tmp_path / "manifest.json"
        listed.write_text("[]", encoding="utf-8")
        with pytest.raises(PluginImportError) as caught:
            read_manifest_name(listed)
        assert caught.value.code == "manifest_not_object"

    def test_the_bounded_reader_refuses_an_oversized_manifest_unread(self, tmp_path):
        import pytest

        from kiro_crew.apps import plugin_import
        from kiro_crew.apps.plugin_import import PluginImportError, read_manifest_name

        big = tmp_path / "manifest.json"
        big.write_text(
            '{"name": "x", "pad": "' + "y" * (plugin_import.MAX_MANIFEST_BYTES + 32) + '"}',
            encoding="utf-8",
        )
        with pytest.raises(PluginImportError) as caught:
            read_manifest_name(big)
        assert caught.value.code == "manifest_too_large"

    def test_a_declared_name_still_reads_and_a_missing_one_is_none(self, tmp_path):
        """The complement, so the refusals above are not true of every manifest."""
        from kiro_crew.apps.plugin_import import read_manifest_name

        named = tmp_path / "a.json"
        named.write_text('{"name": "my-plugin"}', encoding="utf-8")
        assert read_manifest_name(named) == "my-plugin"

        for body in ("{}", '{"name": ""}', '{"name": 7}'):
            anon = tmp_path / "b.json"
            anon.write_text(body, encoding="utf-8")
            assert read_manifest_name(anon) is None, body

    def test_discovery_stops_at_its_entry_ceiling(self, tmp_path, monkeypatch):
        """A wide tree must not fill memory with frontier paths before budgeting.

        Discovery runs before the import budget exists, so the budget cannot bound
        it. The ceiling is lowered for the test rather than building 20,000
        directories, and the assertion is that the walk STOPS, not merely that it
        returns something.
        """
        from kiro_crew.apps import plugin_import

        root = tmp_path / "wide"
        root.mkdir()
        for i in range(40):
            (root / f"d{i:03d}").mkdir()

        monkeypatch.setattr(plugin_import, "MAX_DISCOVERY_ENTRIES", 10)
        seen: list[str] = []
        real_iterdir = plugin_import.Path.iterdir

        def _counting(self):
            seen.append(self.name)
            return real_iterdir(self)

        monkeypatch.setattr(plugin_import.Path, "iterdir", _counting)
        plugin_import._discover_skill_dirs(root)

        # The root listing alone exceeds the lowered ceiling, so the walk must not
        # descend into the 40 children. Without the bound it visits every one.
        assert len(seen) < 40, f"walk did not stop: listed {len(seen)} directories"

    def test_discovery_caps_one_directorys_listing(self, tmp_path, monkeypatch):
        """The per-directory half: one huge directory must not be listed whole."""
        from kiro_crew.apps import plugin_import

        root = tmp_path / "fat"
        root.mkdir()
        for i in range(30):
            (root / f"f{i:03d}").write_text("x", encoding="utf-8")

        monkeypatch.setattr(plugin_import, "MAX_DISCOVERY_ENTRIES", 5)
        consumed = 0
        real_iterdir = plugin_import.Path.iterdir

        def _counting(self):
            nonlocal consumed
            for child in real_iterdir(self):
                consumed += 1
                yield child

        monkeypatch.setattr(plugin_import.Path, "iterdir", _counting)
        plugin_import._discover_skill_dirs(root)

        # Consumed from the iterator, so the count stops near the ceiling rather
        # than at the directory's real size.
        assert consumed <= 10, f"consumed {consumed} entries past a ceiling of 5"

    def test_the_projection_store_refuses_an_oversized_file(self, tmp_path, monkeypatch):
        """The same class, on the one remaining unbounded read this diff adds.

        Not a cited finding: found by sweeping the class across the whole diff
        instead of the module a previous round happened to name.

        The file must hold a VALID row and the ceiling must be lowered beneath it.
        A first version padded past the real ceiling instead, and padding alone
        produces a file with no loadable rows, so the assertion read empty whether
        the ceiling was enforced or not. Mutation caught that; it is why the
        ceiling is patched rather than the file inflated.
        """
        from kiro_crew.eventlog import contrib, grants
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        root = tmp_path / "contrib"
        store = ExternalProjectionStore(root)
        store.publish(
            "member",
            "someone",
            "an-app/card",
            app="an-app",
            value={"a": 1},
            seq=1,
            state_version=0,
            expect_generation=grants.revocation_generation(),
        )
        written = store._path("member", "someone").stat().st_size
        assert written > 0

        monkeypatch.setattr(contrib, "MAX_PROJECTION_STORE_BYTES", written - 1)
        cold = ExternalProjectionStore(root)
        # Starts empty rather than refusing the request, which is how an
        # unreadable file already degrades. Without the ceiling this returns the
        # row that is plainly there on disk.
        assert cold.values("member", "someone") == {}

    def test_a_normal_projection_file_still_loads(self, tmp_path):
        """The complement, so the ceiling is not refusing every file."""
        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        store = ExternalProjectionStore(tmp_path / "contrib")
        store.publish(
            "member",
            "someone",
            "an-app/card",
            app="an-app",
            value={"a": 1},
            seq=1,
            state_version=0,
            expect_generation=grants.revocation_generation(),
        )
        reloaded = ExternalProjectionStore(tmp_path / "contrib")
        row = reloaded.get("member", "someone", "an-app/card")
        assert row is not None and row.value == {"a": 1}

    def test_the_writer_refuses_a_payload_over_the_same_ceiling(self, tmp_path, monkeypatch):
        """The write side of the ceiling the loader above enforces.

        A bound on only the read side is worse than no bound: the writer produces
        a file, reports success, and every later cold load discards it, so the
        unit comes back empty and every app's rows are gone with nothing
        recording the loss. The ceiling is patched rather than the payload
        inflated for the same reason as its sibling -- the real number is 320 MB,
        and a test that builds it measures the machine, not the rule.
        """
        from kiro_crew.eventlog import contrib, grants
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        root = tmp_path / "contrib"
        store = ExternalProjectionStore(root)
        store.publish(
            "member",
            "someone",
            "first/card",
            app="first",
            value={"a": 1},
            seq=1,
            state_version=0,
            expect_generation=grants.revocation_generation(),
        )
        settled = store._path("member", "someone").stat().st_size

        # Above what one row costs and below what two do, so the first publish is
        # a legitimate stored row and only the second crosses the line.
        monkeypatch.setattr(contrib, "MAX_PROJECTION_STORE_BYTES", settled + 4)
        with pytest.raises(contrib.ContribError) as caught:
            store.publish(
                "member",
                "someone",
                "second/card",
                app="second",
                value={"b": "x" * 200},
                seq=1,
                state_version=0,
                expect_generation=grants.revocation_generation(),
            )

        assert caught.value.code == "projection_store_full"
        assert caught.value.status == 409, "an accumulated ceiling, like projection_limit"

        # Rolled back on BOTH sides, which is the property that makes the refusal
        # safe: memory does not hold a row the file lacks, and the file is still
        # the one the loader accepted.
        assert store.get("member", "someone", "second/card") is None
        assert store.get("member", "someone", "first/card") is not None
        assert store._path("member", "someone").stat().st_size == settled

        cold = ExternalProjectionStore(root)
        assert set(cold.values("member", "someone")) == {
            "first/card"
        }, "the file on disk is still one a cold load accepts"

    def test_the_writer_accepts_a_payload_under_the_ceiling(self, tmp_path):
        """The complement, unpatched: the write bound refuses nothing ordinary."""
        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        store = ExternalProjectionStore(tmp_path / "contrib")
        for i in range(8):
            store.publish(
                "member",
                "someone",
                f"an-app/card{i}",
                app="an-app",
                value={"n": i, "pad": "y" * 500},
                seq=1,
                state_version=0,
                expect_generation=grants.revocation_generation(),
            )

        assert len(store.values("member", "someone")) == 8
