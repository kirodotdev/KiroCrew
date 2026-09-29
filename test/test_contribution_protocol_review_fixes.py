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


def _grant(
    monkeypatch,
    *,
    app=APP,
    events=("demoapp/*",),
    projections=("demoapp/*",),
    approved_units=("member",),
):
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
    # A unit kind also needs the operator's approval record, not just the manifest.
    _approved = frozenset(approved_units)
    monkeypatch.setattr(
        "kiro_crew.apps.manager.approved_unit_kinds",
        lambda n: _approved if n == app else frozenset(),
    )
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

    def test_a_corrupt_unit_does_not_discard_another_units_committed_deletion(
        self, tmp_path, monkeypatch
    ):
        """Opus finding: the per-unit guard has to wrap the unit TRANSACTION, not
        only the flush.

        One member's rows are deleted and flushed; a SECOND member's unit file is
        torn, so its ``_unit_transaction`` raises out of the strict load -- which
        happens BEFORE the flush try, so the old placement let the error escape the
        loop, discard the first member's already-committed deletion from
        ``removed``, and get swallowed by teardown as "nothing removed" while the
        rows were in fact gone from disk. The fix must instead raise
        ``ProjectionDeleteIncomplete`` carrying the good delete in ``removed`` AND
        the torn unit in ``failed``.
        """
        store = ExternalProjectionStore(tmp_path / "rows")
        store.publish(
            "member", "good", f"{APP}/card", app=APP, value={"a": 1}, seq=1, state_version=0
        )
        # A second unit on disk whose strict load cannot parse.
        torn = (tmp_path / "rows") / "member" / "torn.json"
        torn.parent.mkdir(parents=True, exist_ok=True)
        torn.write_text("{ not json", encoding="utf-8")

        with pytest.raises(ProjectionDeleteIncomplete) as exc:
            store.delete_app_rows(APP)

        # The good unit's deletion COMMITTED and is reported despite the torn unit.
        assert ("member", "good", f"{APP}/card", 0, 1) in exc.value.removed
        assert "member/torn" in exc.value.failed
        # And it really is gone from disk (not merely reported).
        assert store.get("member", "good", f"{APP}/card") is None

    def test_a_successful_teardown_still_returns_what_it_removed(self, tmp_path):
        store = ExternalProjectionStore(tmp_path / "rows")
        store.publish(
            "member", SLUG, f"{APP}/card", app=APP, value={"a": 1}, seq=1, state_version=0
        )
        removed = store.delete_app_rows(APP)
        assert removed == [("member", SLUG, f"{APP}/card", 0, 1)]
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

    def test_an_oversized_selector_is_refused(self):
        from kiro_crew.eventlog.contrib import ContribError, normalize_schema

        # A raw selector over the RAW cap is REFUSED, not truncated: redact_and_truncate
        # is quadratic, so running it on a multi-KB raw string before cutting would
        # stall the serving loop (Opus 5.5 BLOCK, contrib.py:1379). Same contract as a
        # raw title over the cap.
        with pytest.raises(ContribError) as exc:
            normalize_schema({"kind": "keyvalue", "path": ["x" * 5000]})
        assert "selector" in str(exc.value) and "raw limit" in str(exc.value)

    def test_a_selector_at_the_cap_is_still_truncated_to_120(self):
        from kiro_crew.eventlog.contrib import normalize_schema

        # A selector within the raw cap but over the retained bound is still cut to 120.
        out = normalize_schema({"kind": "keyvalue", "path": ["x" * 4096]})
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
        """Both bounds together are what cap one stored schema's size.

        The title and each selector stay within the RAW-input cap (an oversized raw
        title or selector is a separate refusal, pinned above and in
        test_contribution_protocol) so this exercises the RETAINED bound: title cut
        to 120, path count cut to 32, each selector cut to 120.
        """
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "title": "t" * 400, "path": ["p" * 400] * 200})
        assert len(out["title"]) == 120
        assert len(out["path"]) == 32
        assert all(len(sel) == 120 for sel in out["path"])


# ---------------------------------------------------------------------------
# G7 -- a retained schema field is redacted BEFORE it is bounded
# ---------------------------------------------------------------------------
class TestSchemaFieldsAreRedactedBeforeBounding:
    """Bounding first cuts a credential in half, and the half that survives
    matches nothing.

    The credential patterns are length-anchored -- ``gh[opsur]_[A-Za-z0-9]{30,255}``,
    ``npm_[A-Za-z0-9]{24,}``, a JWT's ``{96,}`` and ``{43}`` segments -- so a
    120-char slice landing inside a secret leaves a prefix the redactor can no
    longer recognise. The read side does redact on egress, but by then it sees
    only the already-cut text, so the order at THIS site is the only thing that
    closes it. Laid out with the secret straddling the bound, which is what makes
    a bound-then-redact reorder go red rather than only a bare slice.
    """

    #: 100 filler characters then the token, so the 120-char bound falls INSIDE it.
    TOKEN = "ghp_" + "A" * 36
    STRADDLE = "x" * 100 + TOKEN

    def test_a_title_straddling_the_bound_does_not_leak_its_prefix(self):
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "title": self.STRADDLE})
        assert "ghp_" not in out["title"], f"credential prefix survived: {out['title'][-40:]!r}"
        assert "REDACTED" in out["title"]
        # The storage bound this site also owns has to keep holding.
        assert len(out["title"]) <= 120

    def test_a_selector_straddling_the_bound_does_not_leak_its_prefix(self):
        from kiro_crew.eventlog.contrib import normalize_schema

        out = normalize_schema({"kind": "keyvalue", "path": [self.STRADDLE]})
        assert "ghp_" not in out["path"][0], f"credential prefix survived: {out['path'][0][-40:]!r}"
        assert "REDACTED" in out["path"][0]
        assert len(out["path"][0]) <= 120

    def test_the_straddle_layout_really_straddles(self):
        """CONTROL for the two pins above.

        If the bound stopped falling inside the token they would pass against a
        bare slice and prove nothing, and the second half states the mechanism
        itself: the cut prefix is what the redactor cannot match.
        """
        from kiro_crew.security import redact_credentials

        assert len(self.STRADDLE) > 120
        assert self.STRADDLE.index("ghp_") < 120 < len(self.STRADDLE)
        assert "ghp_" in redact_credentials(self.STRADDLE[:120])[0]
        assert "ghp_" not in redact_credentials(self.STRADDLE)[0]


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

        assert assert_grants_unchanged("fence-none", None) is None

    def test_an_unchanged_generation_passes(self):
        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import assert_grants_unchanged

        assert assert_grants_unchanged("fence-ok", grants.grant_fence("fence-ok")) is None

    def test_a_moved_generation_is_refused_with_its_own_code(self):
        import pytest

        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ContribError, assert_grants_unchanged

        fence = grants.grant_fence("fence-refused")
        grants.revoke("fence-refused")
        try:
            with pytest.raises(ContribError) as caught:
                assert_grants_unchanged("fence-refused", fence)
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
        fence = grants.grant_fence("pub-app")
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
        fence = grants.grant_fence("schema-app")
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
            expect_generation=grants.grant_fence("live-app"),
        )

        row = store.get("member", "someone", "live-app/card")
        assert row is not None
        assert row.value == {"a": 1}

    def test_every_mutating_commit_path_reads_the_fence(self):
        """Enumeration guard: every suspending route reads the fence, by source.

        Point-fixing one cited line and calling the class closed is how the
        previous rounds' siblings were missed. This fails when a further route is
        added without a fence, rather than waiting for a reviewer to find it. The
        catch-up READ is counted too: its suspension is the same window, and the
        harm there is a revoked app receiving one last page rather than writing.
        """
        import pathlib

        import kiro_crew.dashboard.handlers.eventlog as http_handlers
        import kiro_crew.dashboard.ws as ws_mod
        import kiro_crew.eventlog.contrib as contrib_mod

        store_src = pathlib.Path(contrib_mod.__file__).read_text(encoding="utf-8")
        # Both store-side commits hold the BARRIER inside their per-unit lock, not
        # a bare assertion: the check and the durable write are two statements, and
        # the barrier is what stops a revocation landing between them.
        assert store_src.count("commit_barrier(app, expect_generation)") == 2

        http_src = pathlib.Path(http_handlers.__file__).read_text(encoding="utf-8")
        # All FOUR HTTP handlers read the PER-APP fence before their grant check:
        # append, publish and schema because a revoked app must not commit, and
        # the catch-up read because a revoked app must not receive a final page.
        # Every one of them suspends on an offloaded call, and the suspension is
        # the window, so a route without a fence is a route with the hole. The
        # fence is `grant_fence(app)`, not the bare generation: it folds in the
        # durable disable epoch so a cross-process (file-only) disable is fenced
        # too, not just an in-process one.
        assert http_src.count("fence = grants.grant_fence(app)") == 4
        assert http_src.count("expect_generation=fence") == 2
        # The append COMMITS inside the barrier. The catch-up read keeps the bare
        # assertion, because a read persists nothing: there is no write for a
        # revocation to split it from.
        assert "commit_barrier(app, fence)" in http_src
        assert "assert_grants_unchanged(app, fence)" in http_src

        ws_src = pathlib.Path(ws_mod.__file__).read_text(encoding="utf-8")
        assert ws_src.count("fence = grants.grant_fence(ws_app)") == 1
        assert "assert_grants_unchanged(ws_app, fence)" in ws_src

    def test_a_revoke_waits_for_a_commit_already_past_the_fence(self):
        """The forward ordering: bumping the generation cannot reach a live commit.

        A commit that registered before the bump was authorized in the generation
        being retired, and its durable write is still outstanding. A revoke that
        returned here would let teardown delete rows that the commit writes
        straight back, so the guarantee is not that the counter moved -- it is that
        nothing authorized under the old grant is still unwritten.
        """
        import threading
        import time

        from kiro_crew.eventlog import grants

        app = "drain-order-probe"
        order: list[str] = []
        entered = threading.Event()

        def _revoker():
            entered.set()
            grants.revoke(app)
            order.append("revoke-returned")

        grants.begin_commit(app)
        released = False
        revoker = threading.Thread(target=_revoker, daemon=True)
        revoker.start()
        try:
            assert entered.wait(timeout=5.0)
            # The slot is still held, so the drain cannot be finished. The sleep is
            # what gives a revoke that ignores the slot its chance to return, and
            # returning is the defect.
            time.sleep(0.15)
            assert order == [], "the revoke returned with a commit still in flight"
            order.append("commit-finished")
            grants.end_commit(app)
            released = True
        finally:
            if not released:
                grants.end_commit(app)
            revoker.join(timeout=5.0)
            grants.unrevoke(app)

        assert order == ["commit-finished", "revoke-returned"]

    def test_a_revoke_that_lands_first_makes_the_barrier_write_nothing(self):
        """The reverse ordering, and there is no third: the body never runs.

        Raising is not the property by itself -- a barrier that raised after the
        write would recreate exactly the state teardown deletes -- so the body is
        observed rather than the exception alone.
        """
        import pytest

        from kiro_crew.eventlog import grants
        from kiro_crew.eventlog.contrib import ContribError, commit_barrier

        app = "barrier-reverse-probe"
        fence = grants.revocation_generation()
        grants.revoke(app)
        wrote: list[int] = []
        try:
            with pytest.raises(ContribError) as caught:
                with commit_barrier(app, fence):
                    wrote.append(1)
        finally:
            grants.unrevoke(app)

        assert caught.value.code == "app_revoked"
        assert wrote == []
        # The slot is released on the refusing path too, or this app's teardown
        # waits out the whole drain for a commit that never began.
        with grants._cache_lock:
            assert not grants._inflight.get(app)

    def test_an_unreleased_slot_does_not_hold_teardown_open(self, monkeypatch, caplog):
        """The drain is bounded, and REPORTS whether it completed.

        A caller that dies between ``begin_commit`` and ``end_commit`` must not be
        able to stall a teardown for as long as it likes. The wait is bounded, but
        a timed-out drain returns ``False`` (not ``None``) so the caller keeps the
        revocation raised rather than lifting it beside an outstanding old-authority
        write, and the operator gets a warning naming the app instead of silence.
        """
        import logging

        from kiro_crew.eventlog import grants

        app = "drain-bound-probe"
        monkeypatch.setattr(grants, "_DRAIN_TIMEOUT_SECS", 0.05)
        grants.begin_commit(app)
        try:
            with caplog.at_level(logging.WARNING, logger=grants.logger.name):
                drained = grants.revoke(app)
            assert drained is False, "a timed-out drain must report that it did not complete"
        finally:
            grants.end_commit(app)
            grants.unrevoke(app)

        assert any(
            app in record.getMessage() and "in flight" in record.getMessage()
            for record in caplog.records
        ), "a drain ended by its timeout must warn that commits are still in flight"

    def test_a_completed_drain_reports_true(self, monkeypatch):
        """The clean case: no in-flight commit, so the drain completes and says so.

        The success path lifts the revocation only when this is True, so a
        no-commit revoke must not accidentally report a stall.
        """
        from kiro_crew.eventlog import grants

        app = "drain-clean-probe"
        try:
            assert grants.revoke(app) is True
        finally:
            grants.unrevoke(app)

    def test_success_lift_retains_the_tombstone_while_a_commit_is_outstanding(
        self, monkeypatch, caplog
    ):
        """GPT grants.py:341 -- a timed-out revocation must not permit retired authority.

        The success path of a narrowing replacement calls
        ``_lift_grant_revocation(..., require_drained=True)``. If a commit
        authorized under the retired grant is still outstanding (the drain timed
        out), lifting the tombstone would make the narrowed manifest live BESIDE
        that write, so the write persists against a manifest that does not
        authorize it. The fix RETAINS the revocation instead: the tombstone stays
        raised (``is_revoked`` remains True) until the commit drains, and a later
        lifecycle event lifts it. The rollback path (``require_drained`` False)
        always lifts, because the tree went back to what that write was authorized
        under.
        """
        import logging

        from kiro_crew.apps import manager
        from kiro_crew.eventlog import grants

        app = "retain-on-outstanding-probe"
        # A commit is past the fence and still unwritten.
        grants.begin_commit(app)
        grants.revoke(app)
        try:
            with caplog.at_level(logging.ERROR, logger=manager.logger.name):
                manager._lift_grant_revocation(
                    app, "after replacing its manifest", require_drained=True
                )
            # Retained: the tombstone is STILL raised while the commit is outstanding.
            assert grants.is_revoked(app), (
                "the success lift must retain the revocation while a retired-authority "
                "commit is still outstanding"
            )
            assert any(
                app in r.getMessage() and "NOT lifting" in r.getMessage() for r in caplog.records
            ), "retaining the revocation must be logged loudly"
            # The commit drains; a subsequent settle (or lifecycle event) lifts it.
            grants.end_commit(app)
            manager._lift_grant_revocation(
                app, "after replacing its manifest", require_drained=True
            )
            assert not grants.is_revoked(
                app
            ), "once the outstanding commit drains, the success lift clears the tombstone"
        finally:
            grants.unrevoke(app)

    def test_rollback_lift_clears_the_tombstone_even_with_an_outstanding_commit(self):
        """The rollback path lifts unconditionally: the old manifest is back.

        On rollback the tree and manifest returned to exactly what the outstanding
        write was authorized under, so there is no narrowed manifest for it to
        escape into -- retaining the revocation there would strand the app for no
        gain. ``require_drained`` defaults False, so the tombstone clears at once.
        """
        from kiro_crew.apps import manager
        from kiro_crew.eventlog import grants

        app = "rollback-lift-probe"
        grants.begin_commit(app)
        grants.revoke(app)
        try:
            manager._lift_grant_revocation(app, "after rolling back a failed registration")
            assert not grants.is_revoked(
                app
            ), "the rollback lift must clear the tombstone regardless of outstanding commits"
        finally:
            grants.end_commit(app)
            grants.unrevoke(app)

    def test_register_final_tombstone_lift_is_guarded_to_new_registrations(self):
        """GPT manager.py:3615 -- the final unrevoke must NOT run on the update path.

        ``register_external_app`` ends with a tombstone lift that clears a
        DEPARTING occupant's revocation for a NEW registration. On the UPDATE path
        the replacement already lifted the window revocation through
        ``_lift_grant_revocation(..., require_drained=True)``, which deliberately
        RETAINS the tombstone while a retired-authority commit is still
        outstanding. An UNCONDITIONAL final lift would negate that retention on
        every update -- so the lift must be nested under a ``not existing`` guard.

        Asserted structurally against the AST rather than behaviourally: the defect
        is a call reachable on the update path, and the guard is exactly what makes
        it unreachable there. A raw string match would be fragile; the AST check
        pins the guard without pinning its wording.
        """
        import ast
        import inspect

        from kiro_crew.apps import manager

        src = inspect.getsource(manager.register_external_app)
        tree = ast.parse(src.lstrip())

        def _is_final_unrevoke(node: ast.AST) -> bool:
            # A bare `unrevoke(name)` call statement (the imported-then-called form).
            if not isinstance(node, ast.Expr):
                return False
            call = node.value
            return (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "unrevoke"
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "name"
            )

        # Every `unrevoke(name)` statement, with the set of `if` tests enclosing it.
        guarded: list[bool] = []

        class _Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.if_stack: list[ast.If] = []

            def visit_If(self, node: ast.If) -> None:
                self.if_stack.append(node)
                for child in node.body:
                    self.visit(child)
                self.if_stack.pop()
                for child in node.orelse:
                    self.visit(child)

            def generic_visit(self, node: ast.AST) -> None:
                if _is_final_unrevoke(node):
                    # Guarded iff some enclosing `if` tests `not existing`.
                    ok = any(
                        isinstance(t.test, ast.UnaryOp)
                        and isinstance(t.test.op, ast.Not)
                        and isinstance(t.test.operand, ast.Name)
                        and t.test.operand.id == "existing"
                        for t in self.if_stack
                    )
                    guarded.append(ok)
                super().generic_visit(node)

        _Visitor().visit(tree)
        assert guarded, "register_external_app must still lift a new registration's tombstone"
        assert all(guarded), (
            "the final unrevoke(name) in register_external_app must be guarded by "
            "`if not existing:` so an UPDATE never lifts a tombstone the replacement "
            "deliberately retained while a retired-authority commit is outstanding"
        )

    def test_no_rollback_exit_lifts_the_tombstone_unconditionally(self):
        """GPT manager.py:1775/3469/3583 -- a FAILED rollback must not lift the grant.

        Each failed-update / failed-registration rollback records a `rollback_error`
        / `rollback_errors` on any step that fails. Lifting the contribution
        tombstone (`_lift_grant_revocation` / a bare `unrevoke`) over a
        partially-restored tree serves a grant against state no coherent manifest
        describes. So every rollback-path lift must be GUARDED by an
        `if not <rollback-error>:` (i.e. lift only when the rollback fully
        succeeded). Asserted structurally: a rollback-path `_lift_grant_revocation`
        call whose message mentions 'rolling back' must sit under an `if` that
        tests a rollback-error name.
        """
        import ast
        import inspect

        from kiro_crew.apps import manager

        tree = ast.parse(inspect.getsource(manager))

        def _is_rollback_lift(node: ast.AST) -> bool:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                return False
            call = node.value
            if not (isinstance(call.func, ast.Name) and call.func.id == "_lift_grant_revocation"):
                return False
            # The rollback-path calls carry a "rolling back" message; the success
            # calls ("after replacing ...") are allowed to lift unconditionally.
            return any(
                isinstance(a, ast.Constant)
                and isinstance(a.value, str)
                and "rolling back" in a.value
                for a in call.args
            )

        results: list[bool] = []

        class _V(ast.NodeVisitor):
            def __init__(self) -> None:
                self.ifs: list[ast.If] = []

            def visit_If(self, node: ast.If) -> None:
                self.ifs.append(node)
                for c in node.body:
                    self.visit(c)
                # Keep this `if` on the stack while visiting its `else`: a lift in
                # the `else` of `if <rollback_error>:` is still error-guarded (it
                # runs only when the error is falsy), which is exactly the shape
                # `if rollback_error: log(...) else: lift(...)` uses.
                for c in node.orelse:
                    self.visit(c)
                self.ifs.pop()

            def generic_visit(self, node: ast.AST) -> None:
                if _is_rollback_lift(node):
                    # Guarded iff some enclosing `if` tests a *_error/*_errors name
                    # (the rollback-failure sentinel), i.e. lift only when clean.
                    guarded = any(_mentions_rollback_error(t.test) for t in self.ifs)
                    results.append(guarded)
                super().generic_visit(node)

        def _mentions_rollback_error(test: ast.AST) -> bool:
            for n in ast.walk(test):
                if isinstance(n, ast.Name) and (
                    "rollback_error" in n.id or "rollback_errors" in n.id
                ):
                    return True
            return False

        _V().visit(tree)
        assert results, "expected rollback-path _lift_grant_revocation call(s) to check"
        assert all(results), (
            "every rollback-path _lift_grant_revocation must be guarded by an "
            "`if not <rollback error>:` so a FAILED rollback retains the tombstone "
            "rather than granting against a partially-restored tree"
        )

    def test_reused_name_paths_sweep_orphaned_contribution_rows(self):
        """GPT routes.py:1929 / manager.py:1514 -- a reused app name must not
        inherit prior rows, and an INCOMPLETE sweep is a failed install.

        An incomplete uninstall (unreadable projection store) can leave a prior
        occupant's rows on disk; a same-name install/registration declaring the
        same key would then render them. Both reuse entry points -- `install_app`
        and `register_external_app` -- must sweep orphaned rows for the name
        (`delete_app_rows`) at the reuse point AND, when that sweep is INCOMPLETE
        (`ProjectionDeleteIncomplete`), roll the records back and return a failure
        rather than going live over rows it could not clear. Asserted
        structurally: each function's source both calls `delete_app_rows`, names
        `ProjectionDeleteIncomplete`, and reaches a rollback + failure path from
        it rather than only logging a warning.
        """
        import ast
        import inspect

        from kiro_crew.apps import manager

        def _names(node):
            return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

        def _has_failure_return(node):
            for n in ast.walk(node):
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Call):
                    for kw in n.value.keywords:
                        if (
                            kw.arg == "ok"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is False
                        ):
                            return True
            return False

        def _raises(node, exc_name):
            return any(
                isinstance(n, ast.Raise)
                and isinstance(n.exc, ast.Call)
                and isinstance(n.exc.func, ast.Name)
                and n.exc.func.id == exc_name
                for n in ast.walk(node)
            )

        for fn in (manager.install_app, manager.register_external_app):
            src = inspect.getsource(fn)
            assert "delete_app_rows" in src, (
                f"{fn.__name__} must sweep orphaned contribution rows (delete_app_rows) "
                "at the reuse point so a same-name reinstall cannot inherit them"
            )
            assert (
                "ProjectionDeleteIncomplete" in src
            ), f"{fn.__name__} must handle an INCOMPLETE sweep explicitly, not swallow it"
            tree = ast.parse(src)

            # Collect every `except` clause and classify what it catches. The
            # incomplete sweep must land in a handler that rolls the records back
            # AND returns a failure -- directly (register_external_app catches
            # ProjectionDeleteIncomplete and rolls back inline) or indirectly
            # (install_app re-raises _OrphanRowSweepFailed from the incomplete
            # sweep, caught by an outer handler that rolls back + fails). Reverting
            # either to "log a warning and continue" removes this reachable path
            # and reds the test.
            handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]

            # Path A: a handler that catches ProjectionDeleteIncomplete directly and
            # both rolls back and returns a failure.
            def _catches(h, name):
                t = h.type
                if t is None:
                    return False
                names = t.elts if isinstance(t, ast.Tuple) else [t]
                return any(isinstance(x, ast.Name) and x.id == name for x in names)

            direct = any(
                _catches(h, "ProjectionDeleteIncomplete")
                and "_roll_back_install_records" in _names(h)
                and _has_failure_return(h)
                for h in handlers
            )

            # Path B: the sweep re-raises _OrphanRowSweepFailed (from within a
            # handler that catches ProjectionDeleteIncomplete), and an outer handler
            # catches _OrphanRowSweepFailed, rolls back and returns a failure.
            reraises = any(
                _catches(h, "ProjectionDeleteIncomplete") and _raises(h, "_OrphanRowSweepFailed")
                for h in handlers
            )
            outer_handles = any(
                _catches(h, "_OrphanRowSweepFailed")
                and "_roll_back_install_records" in _names(h)
                and _has_failure_return(h)
                for h in handlers
            )
            indirect = reraises and outer_handles

            assert direct or indirect, (
                f"{fn.__name__}: an INCOMPLETE orphan-row sweep must reach a rollback "
                "(_roll_back_install_records) AND a failed AppResult -- either caught "
                "directly or re-raised as _OrphanRowSweepFailed to an outer handler -- "
                "so the name is never brought live over rows it could not clear"
            )

            # ContribError (a store-level fault -- e.g. an UNREADABLE projection
            # root the store refuses to overwrite) must ALSO reach the same
            # rollback+failure path, not escape the sweep and leave a partial
            # installation. Caught directly (register_external_app rolls back +
            # fails inline) or re-raised as _OrphanRowSweepFailed to the outer
            # handler (install_app).
            assert "ContribError" in src, (
                f"{fn.__name__} must handle a ContribError from the sweep (an unreadable "
                "projection root), not only ProjectionDeleteIncomplete"
            )
            ce_direct = any(
                _catches(h, "ContribError")
                and "_roll_back_install_records" in _names(h)
                and _has_failure_return(h)
                for h in handlers
            )
            ce_reraised = any(
                _catches(h, "ContribError") and _raises(h, "_OrphanRowSweepFailed")
                for h in handlers
            )
            assert ce_direct or (ce_reraised and outer_handles), (
                f"{fn.__name__}: a ContribError from the sweep must reach the rollback "
                "and a failed AppResult (directly or via _OrphanRowSweepFailed), so an "
                "unreadable projection root fails the install closed rather than leaving "
                "it partially live"
            )


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
        import time as _time

        fence = token_auth._scope_fence(app)
        assert fence is not None
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache[app] = (fence, _time.monotonic(), ("/api/probe",))

        # Warm: served from the cache without touching a manifest.
        assert token_auth._app_api_allowlist(app) == ("/api/probe",)

        grants.revoke(app)
        try:
            # The per-app fence bump alone makes the entry stale, so the planted
            # value is gone and the deny-safe read (no such app installed)
            # replaces it.
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
        epoch = grants._durable_disable_epoch(app)
        with grants._cache_lock:
            grants._cache[app] = (generation, epoch, ((), (), ("member",)))
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
        assert body.index("await warm_app_scope(") < body.index("app_token_path_allowed,")
        # And the decision is offloaded UNCONDITIONALLY. A branch keeping a "warm"
        # path inline IS the race: the generation can move between the probe and
        # the call that trusted it, and that call then reads the manifest here.
        assert "_app_scope_is_cold" not in body, (
            "the scope decision branches on a warm probe again; the probe cannot "
            "bind its own answer"
        )
        # One decision site, and it is the offloaded one. Counting the bare name
        # would also count the docstring's mention of it, so the comma is load
        # bearing: it is the call form, `to_thread(app_token_path_allowed, ...)`.
        assert body.count("app_token_path_allowed,") == 1
        assert "app_token_path_allowed(" not in body

    @staticmethod
    def _api_manifest(app: str, prefixes: tuple[str, ...]):
        from kiro_crew.apps.manifest import AppManifest, Permissions

        return AppManifest(
            name=app,
            version="1.0.0",
            displayName=app,
            description="d",
            permissions=Permissions(api=list(prefixes)),
        )

    def test_an_allowlist_resolved_against_replaced_grants_is_discarded(self, monkeypatch):
        """Reading the generation, then the manifest, then answering leaves a gap.

        A revoke or an update landing inside the read means the prefixes describe
        grants that are already replaced, and handing them to the scope check lets
        a withdrawn prefix authorize this request. The value is discarded and
        resolved again against the world that exists.
        """
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.dashboard import token_auth
        from kiro_crew.eventlog import grants

        app = "allowlist-race-probe"
        reads: list[str] = []

        def _manifest_that_moves_once(name):
            reads.append(name)
            if name != app:
                return None
            if len(reads) == 1:
                # A lifecycle event for THIS app lands INSIDE the read, so these
                # prefixes describe grants that are replaced by the time they are
                # returned. It must be this app's own event: a per-app fence only
                # moves for the subject app, which is the unrelated-lifecycle fix.
                grants.invalidate(app)
                return self._api_manifest(app, ("/api/retired",))
            return self._api_manifest(app, ("/api/current",))

        monkeypatch.setattr(manager_mod, "get_app_manifest", _manifest_that_moves_once)
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache.pop(app, None)
        try:
            assert token_auth._app_api_allowlist(app) == ("/api/current",)
            fence = token_auth._scope_fence(app)
            with token_auth._app_perms_lock:
                # Cached under the fence it was resolved in, so the retired value
                # cannot be served to the next request either.
                entry = token_auth._app_perms_cache[app]
                assert entry[0] == fence and entry[2] == ("/api/current",)
        finally:
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache.pop(app, None)

    def test_grants_churning_through_every_read_denies_the_declared_prefixes(
        self, monkeypatch, caplog
    ):
        """The retry is bounded, and the bound fails closed.

        An app whose grants churn can neither hold a request open nor be answered
        from a world that moved. What it gets is its own namespace and none of its
        declared prefixes: narrowed, never opened.
        """
        import logging

        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.dashboard import token_auth
        from kiro_crew.eventlog import grants

        app = "allowlist-churn-probe"
        reads: list[str] = []

        def _manifest_that_always_moves(name):
            reads.append(name)
            # Move THIS app's own per-app fence every read, so its own churn (not
            # an unrelated app's) exhausts the bounded retry and fails closed.
            grants.invalidate(app)
            return self._api_manifest(app, ("/api/wide",))

        monkeypatch.setattr(manager_mod, "get_app_manifest", _manifest_that_always_moves)
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache.pop(app, None)
        try:
            with caplog.at_level(logging.WARNING, logger=token_auth.logger.name):
                assert token_auth._app_api_allowlist(app) == ()
            assert len(reads) == token_auth._ALLOWLIST_RESOLVE_ATTEMPTS
            with token_auth._app_perms_lock:
                # Nothing is cached, so the next request resolves again rather
                # than inheriting this denial.
                assert app not in token_auth._app_perms_cache
        finally:
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache.pop(app, None)

        assert any(
            app in record.getMessage() for record in caplog.records
        ), "denying an app's declared prefixes is an operator-visible event"

    def test_a_cross_process_disable_invalidates_the_permissions_cache(self, monkeypatch):
        """Opus token_auth._scope_generation -- an out-of-process manifest change
        must not leave a token holding withdrawn permissions.api prefixes.

        The perms cache keys on `_scope_fence(app)` = `grants.grant_fence(app)`,
        which folds the durable cross-process disable epoch. A file-only disable in
        another process bumps that epoch, so the cached allowlist goes stale and is
        re-resolved -- a withdrawn prefix cannot keep authorizing a request until a
        restart.
        """
        import time as _time

        from kiro_crew.dashboard import token_auth
        from kiro_crew.eventlog import grants

        app = "perms-xproc-probe"
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache.pop(app, None)
        try:
            fence = token_auth._scope_fence(app)
            assert fence is not None
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache[app] = (fence, _time.monotonic(), ("/api/wide",))
            # Cached: served without a manifest read.
            assert token_auth._app_api_allowlist(app) == ("/api/wide",)

            # A file-only disable in ANOTHER process moves only the durable epoch;
            # the in-process generation is untouched.
            grants.bump_disable_epoch(app)

            # The entry is now stale (fence moved via the epoch), so the deny-safe
            # read (no such app installed) replaces the withdrawn prefixes.
            assert token_auth._app_api_allowlist(app) == ()
        finally:
            grants._epoch_memo = None
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache.pop(app, None)

    def test_the_ttl_backstop_expires_a_stale_perms_entry(self, monkeypatch):
        """A cross-process UPDATE narrowing permissions.api moves neither the
        in-process generation nor the disable epoch, so the TTL is the backstop
        that bounds how long a withdrawn prefix stays cached."""
        import time as _time

        from kiro_crew.dashboard import token_auth

        app = "perms-ttl-probe"
        with token_auth._app_perms_lock:
            token_auth._app_perms_cache.pop(app, None)
        try:
            fence = token_auth._scope_fence(app)
            assert fence is not None
            # Plant an entry whose fence still MATCHES but whose timestamp is older
            # than the TTL: the fence gate would admit it, so only the TTL denies.
            stale_ts = _time.monotonic() - token_auth._APP_PERMS_TTL_SECS - 1
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache[app] = (fence, stale_ts, ("/api/wide",))
            # Re-resolved (deny-safe) rather than served from the expired entry.
            assert token_auth._app_api_allowlist(app) == ()
            assert token_auth._app_scope_is_cold(app) is True
        finally:
            with token_auth._app_perms_lock:
                token_auth._app_perms_cache.pop(app, None)


class TestUnboundedReadsAndWalksAreBounded:
    """Every read this diff adds carries its own ceiling.

    The class the sweep found is "a read with no upper bound on what it loads".
    Within this diff that is the projection store: the loader that reads a
    stored projection off disk and the writer that accepts a payload for one.
    Both are pinned in each direction -- an oversized input is refused, and an
    input under the ceiling still succeeds -- so the ceiling cannot be read as a
    blanket refusal.
    """

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
            expect_generation=grants.grant_fence("an-app"),
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
            expect_generation=grants.grant_fence("an-app"),
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
            expect_generation=grants.grant_fence("first"),
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
                expect_generation=grants.grant_fence("second"),
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
                expect_generation=grants.grant_fence("an-app"),
            )

        assert len(store.values("member", "someone")) == 8


# ---------------------------------------------------------------------------
# Shared-surface pins
#
# Four assertions that each cross from this feature into a surface it shares with
# the member event log: one constant read from two modules, and the socket
# teardown an app update owes. They sit here because this feature owns the side
# that can break.
# ---------------------------------------------------------------------------


def test_the_bound_and_the_door_read_the_same_number():
    """Two constants that disagree would be a payload accepted and then unreadable."""
    from kiro_crew.eventlog import types as _types

    assert contrib.MAX_VALUE_DEPTH == _types.MAX_VALUE_DEPTH


@pytest.fixture
def _isolated_home(tmp_path, monkeypatch):
    """Repoint the data home by ENV VAR, which is what both fences read.

    This file's autouse ``_fresh`` fixture patches ``members.data_home``, which
    does not reach ``crew_log.store``'s own resolution or the file-tool gate, so a
    test whose subject is a real on-disk location takes this one instead. The
    fence-location assertions live in ``test_contrib_fence.py``, because
    ``_fresh`` also points ``contrib.contrib_root`` at a temporary directory and a
    fence assertion made here would only describe that.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


class TestAnUpdateDoesNotLeaveSubscriptionsAuthorized:
    """An app update that narrows `contributions` must not leave a live socket.

    A subscription authorized under the manifest an update replaced otherwise
    keeps streaming a unit the app has lost the right to read. Disabling the app
    closes its sockets; an update that changes the same permissions must too.
    """

    def test_the_update_route_closes_the_apps_event_log_sockets(self):
        """Every SUCCESS path of the update handler must close the sockets.

        The earlier shape of this pin only asked whether the teardown call appeared
        anywhere in the handler, which one branch satisfying it made true for all
        of them -- and the registry branch returned success without closing. So the
        pin counts instead: walk the handler's own returns, keep the ones that
        answer success (a ``web.json_response`` with no ``status=``), and require at
        least as many teardown calls as there are success answers.
        """
        import ast
        import inspect
        import textwrap

        from kiro_crew.apps import routes as routes_mod

        src = textwrap.dedent(inspect.getsource(routes_mod.handle_update_app))
        tree = ast.parse(src)

        successes = 0
        closes = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
                call = node.value
                name = getattr(call.func, "attr", None)
                if name == "json_response" and not any(kw.arg == "status" for kw in call.keywords):
                    successes += 1
            if isinstance(node, ast.Call):
                fn = node.func
                if getattr(fn, "id", None) == "_close_event_log_sockets" or (
                    getattr(fn, "attr", None) == "_close_event_log_sockets"
                ):
                    closes += 1

        assert (
            successes >= 2
        ), f"expected the update handler to keep several success paths, saw {successes}"
        assert closes >= successes, (
            f"{successes} success path(s) but only {closes} event-log socket teardown(s): "
            "a subscription authorized under the manifest an update replaced keeps "
            "streaming on any path that returns without closing"
        )

    def test_the_teardown_helper_actually_closes_the_hubs_sockets(self):
        """The helper the handler calls has to reach the hub.

        Counting calls to a helper proves nothing if the helper is inert, so this
        names the verb: ``close_app`` is the same one disabling an app uses.
        """
        import inspect

        from kiro_crew.apps import routes as routes_mod

        src = inspect.getsource(routes_mod._close_event_log_sockets)
        assert "close_app" in src, (
            "the update path's teardown helper does not close the app's event-log "
            "sockets, so every caller of it is a no-op"
        )


# ---------------------------------------------------------------------------
# R25 -- a unit's projection rows are a cross-process transaction
# ---------------------------------------------------------------------------
class TestProjectionWritesAreSerializedAcrossProcesses:
    """The in-memory map is authority once loaded, which is wrong across processes.

    File-only CLI teardown runs in its OWN process against a live gateway, and
    ``_flush`` rewrites the unit from whatever snapshot the flushing process holds.
    So a snapshot taken before the other side's write goes back whole: one side's
    update is discarded, or a torn-down app's rows are restored into the file the
    drawer renders as authority -- and nothing is left to re-publish and correct it.

    Driven with a REAL second process. The exclusion assertion is an event that must
    never happen (the child finishing while the lock is held), which its own exit
    timing out answers without guessing an interleaving.
    """

    CHILD = (
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('1')\n"
        "from kiro_crew.eventlog.contrib import ExternalProjectionStore\n"
        "store = ExternalProjectionStore(pathlib.Path(sys.argv[3]))\n"
        "store.publish('member', 'someone', 'child-app/card', app='child-app',\n"
        "              value={'from': 'child'}, seq=1, state_version=0)\n"
        "pathlib.Path(sys.argv[2]).write_text('1')\n"
    )

    def _spawn(self, tmp_path, root):
        import subprocess
        import sys

        from kiro_crew.subprocess_utf8 import UTF8_TEXT

        started = tmp_path / "child.started"
        done = tmp_path / "child.done"
        proc = subprocess.Popen(
            [sys.executable, "-c", self.CHILD, str(started), str(done), str(root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tmp_path,
            **UTF8_TEXT,
        )
        return proc, started, done

    @staticmethod
    def _await(path, proc, *, timeout=60.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise AssertionError(f"child exited early with rc={proc.returncode}: {err[-600:]}")
            time.sleep(0.02)
        raise AssertionError(f"child never reached {path.name}")

    def test_another_process_cannot_publish_while_a_unit_transaction_is_held(self, tmp_path):
        import subprocess

        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        root = tmp_path / "contrib"
        store = ExternalProjectionStore(root)
        store.publish(
            "member",
            "someone",
            "parent-app/card",
            app="parent-app",
            value={"from": "parent"},
            seq=1,
            state_version=0,
        )

        proc, started, done = self._spawn(tmp_path, root)
        try:
            with store._unit_transaction("member", "someone"):
                self._await(started, proc)
                with pytest.raises(subprocess.TimeoutExpired):
                    proc.wait(timeout=2.0)
                assert not done.exists(), "another process published while the lock was held"
            assert proc.wait(timeout=60) == 0
            self._await(done, proc)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

        # A FRESH store, so this reads the file rather than either process's memory.
        # Both rows are present, which is the merge: the child re-read inside the
        # lock instead of flushing a snapshot that predated the parent's row.
        fresh = ExternalProjectionStore(root)
        assert set(fresh.values("member", "someone")) == {
            "parent-app/card",
            "child-app/card",
        }

    def test_a_transaction_re_reads_what_another_process_wrote(self, tmp_path):
        """The stale-snapshot case directly: this store loaded the unit BEFORE the
        child wrote, and its next transaction must not flush that older view back.
        """
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        root = tmp_path / "contrib"
        store = ExternalProjectionStore(root)
        store.publish(
            "member",
            "someone",
            "parent-app/card",
            app="parent-app",
            value={"from": "parent"},
            seq=1,
            state_version=0,
        )
        # Loaded and cached here, which is what makes the snapshot stale below.
        assert set(store.values("member", "someone")) == {"parent-app/card"}

        proc, _started, done = self._spawn(tmp_path, root)
        try:
            self._await(done, proc)
            assert proc.wait(timeout=60) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

        store.publish(
            "member",
            "someone",
            "parent-app/second",
            app="parent-app",
            value={"from": "parent"},
            seq=1,
            state_version=0,
        )

        fresh = ExternalProjectionStore(root)
        assert set(fresh.values("member", "someone")) == {
            "parent-app/card",
            "parent-app/second",
            "child-app/card",
        }


# ---------------------------------------------------------------------------
# R27 -- the bounded drain must not freeze the loop, and must not be deleted past
# ---------------------------------------------------------------------------
class TestRevocationIsOffTheLoopAndItsDrainIsRespected:
    """Two halves of the same bound.

    ``revoke`` drains the commits already past the grant fence, and that wait is
    measured in seconds. Run on the event loop it freezes every request and the
    heartbeat; and when it gives up, deleting the app's rows anyway is the one
    ordering that turns a late write into permanent residue -- it lands after the
    rows are gone, with no publisher left to correct it.
    """

    def test_the_revoke_is_offloaded_rather_than_called_on_the_loop(self):
        """Order and shape, by source: an awaited hop in the module's own idiom."""
        import inspect

        from kiro_crew.apps import teardown

        body = inspect.getsource(teardown.retract_contribution_authority)
        assert "run_in_executor(subprocess_executor(), revoke, name)" in body
        # A bare call would be the defect: the drain would run here, on the loop.
        assert "\n        revoke(name)" not in body

    def test_a_drain_that_times_out_still_returns(self, monkeypatch):
        """The bound is real, which is what makes the guard below necessary."""
        from kiro_crew.eventlog import grants

        app = "drain-timeout-probe"
        monkeypatch.setattr(grants, "_DRAIN_TIMEOUT_SECS", 0.05)
        grants.begin_commit(app)
        try:
            grants.revoke(app)
            # Returned with the commit still outstanding: exactly the state the
            # destructive step must refuse to delete under.
            assert grants.outstanding_commits(app) == 1
        finally:
            grants.end_commit(app)
            grants.unrevoke(app)

    def test_the_destructive_step_declines_while_a_commit_is_outstanding(
        self, monkeypatch, tmp_path
    ):
        import asyncio

        from kiro_crew.apps import teardown
        from kiro_crew.eventlog import grants

        app = "late-write-probe"
        store = contrib.get_store()
        store.publish(
            "member",
            "someone",
            f"{app}/card",
            app=app,
            value={"a": 1},
            seq=1,
            state_version=0,
        )

        monkeypatch.setattr(grants, "_DRAIN_TIMEOUT_SECS", 0.05)
        grants.begin_commit(app)
        try:
            grants.revoke(app)
            warnings = asyncio.run(teardown.delete_contribution_rows(app))
        finally:
            grants.end_commit(app)
            grants.unrevoke(app)

        assert any("still being written" in w for w in warnings), warnings
        # The rows are KEPT. That is the recoverable direction: a late write lands
        # beside them and the next retraction takes both.
        assert store.get("member", "someone", f"{app}/card") is not None

    def test_it_deletes_once_the_drain_has_completed(self, tmp_path):
        """The complement: the guard withholds deletion, it does not prevent it."""
        import asyncio

        from kiro_crew.apps import teardown
        from kiro_crew.eventlog import grants

        app = "drained-probe"
        store = contrib.get_store()
        store.publish(
            "member",
            "someone",
            f"{app}/card",
            app=app,
            value={"a": 1},
            seq=1,
            state_version=0,
        )
        grants.revoke(app)
        try:
            assert grants.outstanding_commits(app) == 0
            warnings = asyncio.run(teardown.delete_contribution_rows(app))
        finally:
            grants.unrevoke(app)

        assert not any("still being written" in w for w in warnings), warnings
        assert store.get("member", "someone", f"{app}/card") is None


class TestAnUnreadableProjectionFileIsNotRewritten:
    """The read degrades to empty on purpose -- a contributor re-publishes, so an
    unreadable unit self-heals. A WRITE may not degrade the same way: flushing that
    empty map persists it, erasing every other app's rows in the unit and the bytes
    an operator can still repair.
    """

    def test_an_unreadable_unit_refuses_a_publish_and_keeps_its_bytes(self, tmp_path):
        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        root = tmp_path / "contrib"
        (root / "member").mkdir(parents=True)
        path = root / "member" / "someone.json"
        corrupt = '{"other-app/card": {"value": 1'
        path.write_text(corrupt, encoding="utf-8")

        store = ExternalProjectionStore(root)
        with pytest.raises(ContribError) as caught:
            store.publish(
                "member",
                "someone",
                "new-app/card",
                app="new-app",
                value={"a": 1},
                seq=1,
                state_version=0,
            )

        assert caught.value.code == "projection_store_unreadable"
        # 503 and not a 4xx: no app can repair this, and the refusal exists so an
        # operator still can.
        assert caught.value.status == 503
        assert path.read_text(encoding="utf-8") == corrupt

    def test_an_oversized_unit_refuses_a_publish_and_keeps_its_bytes(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import contrib as contrib_mod
        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        root = tmp_path / "contrib"
        (root / "member").mkdir(parents=True)
        path = root / "member" / "someone.json"
        payload = '{"other-app/card": {"value": "' + "x" * 400 + '"}}'
        path.write_text(payload, encoding="utf-8")
        monkeypatch.setattr(contrib_mod, "MAX_PROJECTION_STORE_BYTES", 100)

        store = ExternalProjectionStore(root)
        with pytest.raises(ContribError) as caught:
            store.publish(
                "member",
                "someone",
                "new-app/card",
                app="new-app",
                value={"a": 1},
                seq=1,
                state_version=0,
            )

        assert caught.value.code == "projection_store_unreadable"
        assert path.read_text(encoding="utf-8") == payload

    def test_teardown_deletion_refuses_an_unreadable_unit_rather_than_blanking_it(self, tmp_path):
        """The destructive path is the one that would erase the most: every app's
        rows in the unit, not just the torn-down app's.

        The unit is reported through ``ProjectionDeleteIncomplete.failed`` (nothing
        landed, so ``removed`` is empty) rather than leaking the raw strict-load
        error out of the loop: the per-unit guard wraps the transaction itself, so
        one torn unit is recorded and never discards another unit's committed
        delete. Either way the corrupt file keeps its bytes -- the point of the
        refusal.
        """
        from kiro_crew.eventlog.contrib import ExternalProjectionStore, ProjectionDeleteIncomplete

        root = tmp_path / "contrib"
        (root / "member").mkdir(parents=True)
        path = root / "member" / "someone.json"
        corrupt = '{"gone-app/card": {"value": 1'
        path.write_text(corrupt, encoding="utf-8")

        store = ExternalProjectionStore(root)
        with pytest.raises(ProjectionDeleteIncomplete) as caught:
            store.delete_app_rows("gone-app")

        assert caught.value.removed == []
        assert caught.value.failed == ["member/someone"]
        assert path.read_text(encoding="utf-8") == corrupt

    def test_a_read_still_degrades_to_empty(self, tmp_path):
        """The READ-ONLY NORMALIZATION case. Unchanged, and deliberately: a corrupt
        unit must not fail every request for it, and a contributor's next publish
        re-populates what it owns."""
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        root = tmp_path / "contrib"
        (root / "member").mkdir(parents=True)
        (root / "member" / "someone.json").write_text('{"a/b": {"value": 1', encoding="utf-8")

        store = ExternalProjectionStore(root)
        assert store.values("member", "someone") == {}
        assert store.get("member", "someone", "a/b") is None

    def test_a_read_skips_only_the_malformed_row(self, tmp_path):
        """Read-only normalization at ROW granularity: the valid neighbour is served."""
        from kiro_crew.eventlog.contrib import ExternalProjectionStore

        root = tmp_path / "contrib"
        (root / "member").mkdir(parents=True)
        (root / "member" / "someone.json").write_text(
            '{"a/bad": {"seq": 1}, "a/good": {"value": 2, "seq": 1, "app": "a"}}',
            encoding="utf-8",
        )

        store = ExternalProjectionStore(root)
        served = store.values("member", "someone")
        assert set(served) == {"a/good"}

    def _corrupt_unit(self, tmp_path, body):
        root = tmp_path / "contrib"
        (root / "member").mkdir(parents=True)
        path = root / "member" / "someone.json"
        path.write_text(body, encoding="utf-8")
        return root, path

    def _publish(self, store, app="new-app"):
        store.publish(
            "member",
            "someone",
            f"{app}/card",
            app=app,
            value={"a": 1},
            seq=1,
            state_version=0,
        )

    def test_a_malformed_root_refuses_a_publish(self, tmp_path):
        """MALFORMED ROOT: valid JSON, but a list where the row map belongs."""
        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        root, path = self._corrupt_unit(tmp_path, '["a/b"]')
        store = ExternalProjectionStore(root)

        with pytest.raises(ContribError) as caught:
            self._publish(store)

        assert caught.value.code == "projection_store_unreadable"
        assert "not an object" in str(caught.value)
        assert path.read_text(encoding="utf-8") == '["a/b"]'

    def test_a_malformed_row_refuses_a_publish_though_the_file_parses(self, tmp_path):
        """MALFORMED ENTRY: the file and root are fine; one row has no ``value``."""
        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        body = '{"other-app/card": {"seq": 1, "app": "other-app"}}'
        root, path = self._corrupt_unit(tmp_path, body)
        store = ExternalProjectionStore(root)

        with pytest.raises(ContribError) as caught:
            self._publish(store)

        assert caught.value.code == "projection_store_unreadable"
        assert "other-app/card" in str(caught.value)
        assert path.read_text(encoding="utf-8") == body

    def test_an_unrelated_publish_is_what_would_have_destroyed_the_row(self, tmp_path):
        """UNRELATED MUTATION: a publish about one app deletes another app's row."""
        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        body = (
            '{"other-app/card": {"seq": 1, "app": "other-app"}, '
            '"third-app/card": {"value": 9, "seq": 1, "app": "third-app"}}'
        )
        root, path = self._corrupt_unit(tmp_path, body)
        store = ExternalProjectionStore(root)

        with pytest.raises(ContribError):
            self._publish(store, app="unrelated-app")

        assert path.read_text(encoding="utf-8") == body
        # The valid neighbour that shared the file is still served.
        assert set(store.values("member", "someone")) == {"third-app/card"}

    def test_a_publish_over_the_malformed_row_itself_also_refuses(self, tmp_path):
        """SAME-ENTRY: the caller would overwrite this very key, so nothing would be
        lost. It still refuses -- a writer that reads the store reads it whole."""
        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        body = '{"other-app/card": {"seq": 1, "app": "other-app"}}'
        root, path = self._corrupt_unit(tmp_path, body)
        store = ExternalProjectionStore(root)

        with pytest.raises(ContribError):
            self._publish(store, app="other-app")

        assert path.read_text(encoding="utf-8") == body


class TestTheHttpBoundaryHonoursEveryMappedStatus:
    """The code-to-status mapping is only half a contract; the handler's ladder is the
    other half. A code whose status has no rung falls through to 400 and blames the
    client for something it did not do.
    """

    def test_every_mapped_status_has_a_rung(self):
        from kiro_crew.dashboard.handlers.eventlog import _err
        from kiro_crew.eventlog.contrib import STATUS_FOR_CODE

        wrong = {}
        for code, expected in sorted(STATUS_FOR_CODE.items()):
            got = _err(code, "x").status
            if got != expected:
                wrong[code] = (expected, got)
        assert not wrong, f"codes whose wire status does not match the mapping: {wrong}"

    def test_the_unreadable_store_code_answers_503(self):
        """The specific code this round adds, named so a regression is legible."""
        from kiro_crew.dashboard.handlers.eventlog import _err

        assert _err("projection_store_unreadable", "x").status == 503

    def test_an_unmapped_code_still_falls_back_to_400(self):
        from kiro_crew.dashboard.handlers.eventlog import _err

        assert _err("no_such_code_exists", "x").status == 400


# ---------------------------------------------------------------------------
# F1 -- strict load must not COERCE a malformed row it re-persists
# ---------------------------------------------------------------------------
class TestStrictLoadRefusesCoercibleRows:
    """A reader drops a row it cannot parse (a contributor re-publishes); a
    MUTATION rewrites the file whole, so it must refuse a row whose ordering
    fields or schema it would otherwise silently rewrite. ``int()`` accepts a
    bool (``True`` is 1) and a numeric string, and a non-dict schema was dropped
    -- strict mode rejects all three so a whole-file flush never persists a value
    the caller never stored."""

    def test_strict_refuses_a_bool_seq(self):
        with pytest.raises(ValueError, match="'seq' must be an integer"):
            contrib.ExternalRow.from_dict(
                {"value": 1, "seq": True, "stateVersion": 0, "app": "demoapp"},
                strict=True,
            )

    def test_strict_refuses_a_string_seq(self):
        with pytest.raises(ValueError, match="'seq' must be an integer"):
            contrib.ExternalRow.from_dict(
                {"value": 1, "seq": "7", "stateVersion": 0, "app": "demoapp"},
                strict=True,
            )

    def test_strict_refuses_a_bool_state_version(self):
        with pytest.raises(ValueError, match="'stateVersion' must be an integer"):
            contrib.ExternalRow.from_dict(
                {"value": 1, "seq": 1, "stateVersion": True, "app": "demoapp"},
                strict=True,
            )

    def test_strict_refuses_a_non_dict_schema(self):
        with pytest.raises(ValueError, match="'schema' must be an object or absent"):
            contrib.ExternalRow.from_dict(
                {
                    "value": 1,
                    "seq": 1,
                    "stateVersion": 0,
                    "app": "demoapp",
                    "schema": ["not", "a", "dict"],
                },
                strict=True,
            )

    def test_strict_accepts_a_clean_row_unchanged(self):
        row = contrib.ExternalRow.from_dict(
            {
                "value": {"a": 1},
                "seq": 5,
                "stateVersion": 2,
                "app": "demoapp",
                "schema": {"title": "t"},
            },
            strict=True,
        )
        assert row is not None
        assert row.seq == 5 and row.state_version == 2
        assert row.schema == {"title": "t"}

    def test_the_lenient_reader_still_drops_an_unparseable_row(self):
        # The reader never raises -- a row it cannot parse is skipped so one
        # unit's rot does not fail a read that also spans another app's rows. A
        # list seq is unparseable (int() raises), so it is dropped; only strict
        # mode additionally rejects a bool/string seq the reader still coerces.
        assert (
            contrib.ExternalRow.from_dict(
                {"value": 1, "seq": ["nope"], "stateVersion": 0, "app": "demoapp"},
                strict=False,
            )
            is None
        )

    def test_the_lenient_reader_coerces_a_bool_seq(self):
        # int(True) == 1: the reader keeps its long-standing coercion so a read
        # never fails; the tightening is scoped to the strict mutation path only.
        row = contrib.ExternalRow.from_dict(
            {"value": 1, "seq": True, "stateVersion": 0, "app": "demoapp"},
            strict=False,
        )
        assert row is not None and row.seq == 1

    def test_the_lenient_reader_drops_a_non_dict_schema_to_none(self):
        row = contrib.ExternalRow.from_dict(
            {"value": 1, "seq": 1, "stateVersion": 0, "app": "demoapp", "schema": 5},
            strict=False,
        )
        assert row is not None and row.schema is None


class TestAFileOnlyDisableDropsTheWarmGrantSynchronously:
    """GPT hook_reconcile.py:335 -- a file-only disable must revoke the running
    gateway's warm grant WITHOUT waiting for the reconciler poll.

    `_generation` is process-local: it dislodges a warm cache entry only for a
    lifecycle event THIS process ran. A file-only CLI `disable` runs in a DIFFERENT
    process (the no-AF_UNIX / Windows case), flipping `enabled=False` on disk but
    never bumping a running gateway's `_generation` -- so a warm live grant would
    keep authorizing the disabled app's token until the 15-second poll.

    `disable_app` now bumps a DURABLE cross-process disable epoch before it returns,
    and the grant cache honours a warm entry only while the epoch is unchanged. The
    running gateway therefore denies the disabled app on its very next grant check,
    not at the next poll.
    """

    def _reset(self):
        with grants._cache_lock:
            grants._cache.clear()
            grants._revoked.clear()
        grants._epoch_memo = None

    def test_bumping_the_epoch_invalidates_a_warm_grant_without_a_generation_bump(
        self, monkeypatch
    ):
        from kiro_crew.apps.manifest import AppManifest, Contributions

        app = "epoch-probe"
        self._reset()

        enabled = {"v": True}
        manifest = AppManifest(
            name=app,
            version="1.0.0",
            displayName=app,
            description="d",
            contributions=Contributions(
                events=[f"{app}/*"], projections=[f"{app}/*"], units=["member"]
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest", lambda n: manifest if n == app else None
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager.is_app_enabled", lambda n: enabled["v"] and n == app
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager.approved_unit_kinds",
            lambda n: frozenset({"member"}) if n == app else frozenset(),
        )

        # Warm the cache: the app is enabled, so it holds a live grant.
        grants.warm(app)
        assert grants.may_publish(app, "member", f"{app}/count") is True
        assert grants.is_cached(app) is True

        # A file-only disable in ANOTHER process: flip the on-disk enabled state
        # and bump the DURABLE epoch. Crucially, do NOT touch `_generation` or the
        # in-process `_revoked` set -- those are exactly what a cross-process
        # disable cannot reach, and the point is that the epoch alone suffices.
        generation_before = grants.revocation_generation()
        enabled["v"] = False
        grants.bump_disable_epoch(app)
        assert grants.revocation_generation() == generation_before, (
            "the test must not move the in-process generation -- the epoch is the "
            "cross-process signal under test"
        )

        # The warm entry is stale (epoch moved), so the grant path re-reads, sees
        # the disk-disabled app, and denies. Without the durable-epoch gate the
        # stale warm live grant would still answer True.
        assert grants.is_cached(app) is False, "the epoch bump must invalidate the warm entry"
        assert grants.may_publish(app, "member", f"{app}/count") is False, (
            "a file-only disable left the warm grant live -- the disabled app's token "
            "can still publish until the poll"
        )
        self._reset()

    def test_disable_app_bumps_the_durable_epoch(self, monkeypatch, tmp_path):
        """The bump is wired into `disable_app` itself, so BOTH the gateway handler
        and the CLI file-only path emit the cross-process signal."""
        from kiro_crew.apps import manager

        app = "epoch-wiring-probe"
        self._reset()
        before = grants._durable_disable_epoch(app)
        assert before == 0

        # Drive the real disable_app against an installed+enabled app record.
        meta = manager.InstalledApp(
            name=app,
            version="1.0.0",
            displayName=app,
            enabled=True,
            source="test",
        )
        monkeypatch.setattr(manager, "_read_installed", lambda n: meta if n == app else None)
        written: dict = {}
        monkeypatch.setattr(
            manager,
            "_write_installed",
            lambda n, m: written.update({"name": n, "enabled": m.enabled}),
        )

        result = manager.disable_app(app)
        assert result.ok
        assert written.get("enabled") is False
        after = grants._durable_disable_epoch(app)
        assert after == before + 1, "disable_app must bump the durable disable epoch"
        self._reset()


class TestTheDisableEpochFileIsProtectedAndSerialized:
    """GPT grants.py:108 -- the durable epoch's read-modify-write must not lose
    concurrent updates, and its file/lock must be owner-only and precreated.

    The bump reads the map, increments one app's entry, and atomically replaces
    the file. Without a lock two concurrent bumps (two disables in flight, or the
    gateway's own disable racing a CLI disable in another process) both read the
    same base and the second replace clobbers the first's increment -- an app that
    stayed authorized because its epoch never moved. The whole read-modify-write is
    serialized under an exclusive cross-process file lock, and both files are
    created owner-only.
    """

    def _reset(self):
        grants._epoch_memo = None

    def test_concurrent_bumps_do_not_lose_an_update(self, monkeypatch):
        import threading

        self._reset()
        apps = [f"epoch-conc-{i}" for i in range(12)]
        barrier = threading.Barrier(len(apps))

        def bump(name: str) -> None:
            barrier.wait()  # maximise overlap on the read-modify-write
            grants.bump_disable_epoch(name)

        threads = [threading.Thread(target=bump, args=(a,)) for a in apps]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        grants._epoch_memo = None  # drop the memo so this reads from disk
        # Every distinct app's increment survived -- none was lost to a clobbering
        # replace. Without the lock, concurrent bumps of DISTINCT keys drop entries
        # because each writer rebuilds the whole map from its own stale read.
        for a in apps:
            assert grants._durable_disable_epoch(a) == 1, (
                f"{a}'s bump was lost to a concurrent writer -- the read-modify-write "
                "is not serialized"
            )
        self._reset()

    def test_repeated_bumps_of_one_app_all_count(self, monkeypatch):
        import threading

        self._reset()
        app = "epoch-repeat"
        n = 20
        barrier = threading.Barrier(n)

        def bump() -> None:
            barrier.wait()
            grants.bump_disable_epoch(app)

        threads = [threading.Thread(target=bump) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        grants._epoch_memo = None
        assert grants._durable_disable_epoch(app) == n, (
            "concurrent bumps of the SAME app lost increments -- the increment is "
            "not read-modify-write under a lock"
        )
        self._reset()

    def test_the_epoch_file_and_lock_are_owner_only(self):
        import os
        import stat
        import sys

        self._reset()
        grants.bump_disable_epoch("epoch-perms")
        if sys.platform == "win32":
            return  # POSIX mode bits are a no-op on Windows
        for path in (grants._grant_epochs_path(), grants._grant_epochs_lock_path()):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode & 0o077 == 0, f"{path} is group/world accessible: {oct(mode)}"
        self._reset()

    def test_the_epoch_file_lives_under_the_fenced_vault_dir(self):
        """GPT grants.py:111 -- a confined app must not be able to write its own
        epoch (which would un-disable it).

        The epoch file and its lock live under the agent-fenced ``.vault``
        directory, which `security.paths` denylists for READ and WRITE to every
        app-mediated channel and the sandbox bind-mounts empty over. Placing the
        file there is what puts it in both governance sets; asserting the path
        keeps a refactor from silently moving it back to the writable config root.
        """
        for path in (grants._grant_epochs_path(), grants._grant_epochs_lock_path()):
            parts = path.parts
            assert ".vault" in parts, f"{path} is not under the fenced .vault dir"

    def test_the_vault_dir_is_a_denylisted_sensitive_path(self):
        """The governance set actually fences ``.vault`` -- so the file placed
        there is covered, not merely named after a directory that sounds fenced."""
        from kiro_crew.security import paths as security_paths

        assert any(
            str(entry).endswith(".vault") for entry in security_paths._SENSITIVE_HOME_DIRS
        ), (
            "the .vault dir the epoch file relies on is not in the security "
            "sensitive-path denylist"
        )


class TestTheCommitEpochLockClosesTheFenceRace:
    """GPT grants.py:554 -- begin_commit reads the epoch WITHOUT the epoch lock,
    so a cross-process bump can land between that read and the durable write.

    `commit_epoch_lock` holds the epoch file's SHARED lock across the write and
    re-reads the epoch after acquiring it, so a bump (which needs the EXCLUSIVE
    lock) cannot move the epoch under an in-flight commit, and a bump that already
    landed is caught by the post-acquisition fence check. Failures fail closed.
    """

    def _reset(self):
        with grants._cache_lock:
            grants._cache.clear()
            grants._revoked.clear()
        grants._epoch_memo = None

    def test_the_lock_refuses_when_the_epoch_moved_before_entry(self):
        self._reset()
        import pytest

        app = "commit-lock-moved"
        captured = grants.grant_fence(app)
        grants.bump_disable_epoch(app)  # a disable lands before the commit enters
        with pytest.raises(grants.EpochFenceUnavailable):
            with grants.commit_epoch_lock(app, captured):
                pass  # pragma: no cover - the enter must raise
        self._reset()

    def test_the_lock_admits_a_matching_fence(self):
        self._reset()
        app = "commit-lock-ok"
        captured = grants.grant_fence(app)
        entered = {"v": False}
        with grants.commit_epoch_lock(app, captured):
            entered["v"] = True
        assert entered["v"], "a matching fence must admit the commit"
        self._reset()

    def test_an_unreadable_epoch_fails_closed(self, monkeypatch):
        self._reset()
        import pytest

        app = "commit-lock-unreadable"
        captured = 999  # any non-None fence
        monkeypatch.setattr(grants, "_durable_disable_epoch", lambda a: None)
        with pytest.raises(grants.EpochFenceUnavailable):
            with grants.commit_epoch_lock(app, captured):
                pass  # pragma: no cover
        self._reset()

    def test_a_none_fence_takes_no_lock(self):
        """The unfenced caller (host-side write, test) is unchanged."""
        self._reset()
        entered = {"v": False}
        with grants.commit_epoch_lock("commit-lock-none", None):
            entered["v"] = True
        assert entered["v"]
        self._reset()

    def test_a_bump_serializes_with_a_held_commit_lock(self):
        """The exclusive bump cannot proceed while a shared commit lock is held,
        so the epoch cannot move under an in-flight commit."""
        self._reset()
        import threading
        import time

        app = "commit-lock-serialize"
        captured = grants.grant_fence(app)
        bump_done = threading.Event()
        order: list = []

        def bumper():
            # Give the main thread time to enter the shared lock first.
            time.sleep(0.2)
            order.append("bump-start")
            grants.bump_disable_epoch(app)
            order.append("bump-done")
            bump_done.set()

        t = threading.Thread(target=bumper)
        with grants.commit_epoch_lock(app, captured):
            t.start()
            # Hold the shared lock briefly; the exclusive bump must wait.
            time.sleep(0.5)
            order.append("commit-inside")
            assert not bump_done.is_set(), (
                "the bump acquired the exclusive lock while a commit held the shared "
                "lock -- the epoch could move under an in-flight commit"
            )
        t.join(timeout=10)
        # After the commit released, the bump completes.
        assert order[0] == "bump-start"
        assert "commit-inside" in order
        assert order[-1] == "bump-done"
        self._reset()


class TestTheDurableEpochFencesCommitsAndDelivery:
    """GPT contrib.py:324 -- the durable epoch must fence in-flight commits and
    live delivery, not only invalidate the read cache.

    A cross-process disable moves only the durable epoch, so a request that fenced
    on the in-process generation alone would still commit its write, and a warm
    subscription would still receive frames, after the disable. The fence folds the
    durable epoch in (`grant_fence`), so both the commit barrier and the delivery
    re-check refuse once the epoch moves.
    """

    def _reset(self):
        with grants._cache_lock:
            grants._cache.clear()
            grants._revoked.clear()
        grants._epoch_memo = None

    def test_grant_fence_moves_when_only_the_durable_epoch_moves(self):
        self._reset()
        app = "fence-epoch-probe"
        before = grants.grant_fence(app)
        generation_before = grants.revocation_generation()
        grants.bump_disable_epoch(app)
        assert (
            grants.revocation_generation() == generation_before
        ), "the test must move ONLY the durable epoch"
        after = grants.grant_fence(app)
        assert after != before, (
            "grant_fence did not move when the durable epoch did -- a cross-process "
            "disable would not fence a commit or delivery"
        )
        self._reset()

    def test_an_unreadable_epoch_makes_the_fence_fail_closed(self, monkeypatch):
        self._reset()
        app = "fence-unreadable"
        monkeypatch.setattr(grants, "_durable_disable_epoch", lambda a: None)
        fence = grants.grant_fence(app)
        assert fence == grants._FENCE_EPOCH_UNREADABLE
        # A commit comparing a real captured fence against this sentinel refuses.
        import pytest

        from kiro_crew.eventlog.contrib import ContribError, assert_grants_unchanged

        with pytest.raises(ContribError) as caught:
            assert_grants_unchanged(app, 12345)  # any captured value != sentinel
        assert caught.value.code == "app_revoked"
        self._reset()

    def test_a_commit_is_refused_when_the_durable_epoch_moved_mid_request(self, tmp_path):
        """The commit path: a fence captured before a cross-process disable is
        refused at commit even though the in-process generation never moved -- and
        the SAME captured fence commits cleanly when no disable intervenes, so the
        refusal is the epoch move, not a fence-shape mismatch."""
        self._reset()
        import pytest

        from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore

        app = "fence-commit-probe"

        # Control: a fence captured and committed with NO disable in between lands.
        # This is what reds if begin_commit stops folding the epoch in (its return
        # would then differ from the composite the handler captured).
        ok_store = ExternalProjectionStore(tmp_path / "ok")
        ok_store.publish(
            "member",
            "someone",
            f"{app}/card",
            app=app,
            value={"a": 1},
            seq=1,
            state_version=0,
            expect_generation=grants.grant_fence(app),
        )
        assert ok_store.get("member", "someone", f"{app}/card") is not None

        store = ExternalProjectionStore(tmp_path / "contrib")
        captured = grants.grant_fence(app)

        # A file-only disable in another process: only the durable epoch moves.
        generation_before = grants.revocation_generation()
        grants.bump_disable_epoch(app)
        assert grants.revocation_generation() == generation_before

        with pytest.raises(ContribError) as caught:
            store.publish(
                "member",
                "someone",
                f"{app}/card",
                app=app,
                value={"a": 1},
                seq=1,
                state_version=0,
                expect_generation=captured,
            )
        assert caught.value.code == "app_revoked"
        # Nothing was written under the stale fence.
        assert store.get("member", "someone", f"{app}/card") is None
        self._reset()

    def test_delivery_denies_a_socket_whose_app_epoch_moved_after_subscribe(self):
        """The delivery path: `_live_peers` drops a peer whose captured fence no
        longer matches, which a cross-process disable causes via the epoch."""
        self._reset()
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        app = "fence-delivery-probe"

        class _WS:
            """Hashable ws stand-in with the dict-style item API the hub uses."""

            def __init__(self):
                self._d: dict = {}

            def __setitem__(self, k, v):
                self._d[k] = v

            def __getitem__(self, k):
                return self._d[k]

            def get(self, k, default=None):
                return self._d.get(k, default)

        hub = EventLogHub()
        ws = _WS()
        ws["_app"] = app
        ws["_app_fence"] = grants.grant_fence(app)
        hub.subscribe(ws, "member", "someone")

        # Present before the disable.
        assert ws in hub._live_peers("member", "someone")

        # File-only disable: only the durable epoch moves.
        grants.bump_disable_epoch(app)
        assert ws not in hub._live_peers(
            "member", "someone"
        ), "a warm subscription kept receiving frames after a cross-process disable"
        self._reset()


class TestTheFenceIsPerAppNotGlobal:
    """GPT eventlog_ws.py:397 -- an UNRELATED app's lifecycle must not invalidate
    this app's commit fence or permanently silence its live subscription.

    The fence folds a PER-APP generation, not a global counter: only the subject
    app's own revoke/unrevoke/invalidate (or its durable epoch) moves its fence.
    A global counter bumped by every app's lifecycle would make one app's
    enable/disable refuse every other app's in-flight commit and deny delivery to
    every other app's socket forever.
    """

    def _reset(self):
        with grants._cache_lock:
            grants._cache.clear()
            grants._revoked.clear()
            grants._app_generation.clear()
        grants._epoch_memo = None

    def test_an_unrelated_apps_lifecycle_does_not_move_this_apps_fence(self):
        self._reset()
        mine = "fence-mine"
        other = "fence-other"
        before = grants.grant_fence(mine)

        # Every lifecycle event on an UNRELATED app.
        grants.revoke(other)
        grants.unrevoke(other)
        grants.invalidate(other)

        assert grants.grant_fence(mine) == before, (
            "an unrelated app's lifecycle moved this app's fence -- its commits "
            "would be refused and its live subscription permanently silenced"
        )
        # And this app's OWN lifecycle still moves its fence.
        grants.revoke(mine)
        assert grants.grant_fence(mine) != before
        self._reset()

    def test_delivery_survives_an_unrelated_disable(self):
        self._reset()
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        mine = "delivery-mine"
        other = "delivery-other"

        class _WS:
            def __init__(self):
                self._d: dict = {}

            def __setitem__(self, k, v):
                self._d[k] = v

            def __getitem__(self, k):
                return self._d[k]

            def get(self, k, default=None):
                return self._d.get(k, default)

        hub = EventLogHub()
        ws = _WS()
        ws["_app"] = mine
        ws["_app_fence"] = grants.grant_fence(mine)
        hub.subscribe(ws, "member", "someone")
        assert ws in hub._live_peers("member", "someone")

        # An unrelated app is disabled/enabled -- this socket must keep receiving.
        grants.revoke(other)
        grants.unrevoke(other)
        assert ws in hub._live_peers(
            "member", "someone"
        ), "an unrelated app's lifecycle silenced this app's live subscription"
        self._reset()


class TestFenceAdmitsFailsClosedOnUnavailable:
    """GPT grants.py:523 -- an unreadable fence must DENY every comparison,
    including unavailable-vs-unavailable, not compare equal and fail open.

    A fixed sentinel equals itself, so a bare ``==`` would ADMIT a commit or a
    delivery whose fence was unreadable both when captured and when re-read.
    `fence_admits` treats the sentinel as unavailable and denies it on either
    side.
    """

    def test_unavailable_on_either_side_denies(self):
        U = grants._FENCE_EPOCH_UNREADABLE
        assert (
            grants.fence_admits(U, U) is False
        ), "unavailable-vs-unavailable admitted -- an unreadable fence failed OPEN"
        assert grants.fence_admits(U, 5) is False
        assert grants.fence_admits(5, U) is False

    def test_two_real_equal_fences_admit(self):
        assert grants.fence_admits(1234, 1234) is True

    def test_two_real_different_fences_deny(self):
        assert grants.fence_admits(1234, 5678) is False

    def test_a_grant_fence_over_an_unreadable_epoch_is_denied_both_ways(self, monkeypatch):
        app = "fence-unreadable-admit"
        monkeypatch.setattr(grants, "_durable_disable_epoch", lambda a: None)
        captured = grants.grant_fence(app)
        current = grants.grant_fence(app)
        assert captured == grants._FENCE_EPOCH_UNREADABLE
        assert current == grants._FENCE_EPOCH_UNREADABLE
        # Both sides unreadable, and a bare != would call them equal (admit); the
        # helper denies.
        assert grants.fence_admits(captured, current) is False
