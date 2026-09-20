"""Contribution protocol: manifest declaration, grants, store, budget, routes.

Covers ``docs/system-specs/modules/contribution-protocol.md`` section by
section, and the two places a bug here would be SILENT rather than loud:

* a contributed row seeded at the wrong seq (``§5`` + the store's
  higher-seq-wins rule) freezes the card at its baseline with no error anywhere;
* a grant that survives a disable (``§6``) lets a stopped app keep writing.

Isolation: every test re-roots BOTH the members space and the contribution
store at a fresh ``tmp_path`` and drops the cached singletons, so no test reads
another's rows. The aiohttp fixtures mirror
``test_members_eventlog_wiring.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import members
from kiro_crew.apps.manifest import AppManifest, Contributions
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.eventlog import contrib, grants, types
from kiro_crew.eventlog.contrib import ContribError, ExternalProjectionStore, get_store, set_store
from kiro_crew.eventlog.service import get_service, set_service

CREW = "code-reviewer"
APP = "demoapp"
SLUG = "code-reviewer"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """Re-root the members space AND the contribution store at tmp_path.

    ``contrib_root`` is monkeypatched rather than only calling ``set_store``:
    ``get_store()`` rebuilds its singleton whenever the root it was created for
    moves, so an injected store rooted elsewhere would be discarded on the first
    call and the test would write to the real data home.
    """
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
    units=("member",),
    enabled=True,
):
    """Make ``grants`` answer as if *app* declared these contributions."""
    manifest = AppManifest(
        name=app,
        version="1.0.0",
        displayName=app,
        description="d",
        contributions=Contributions(
            events=list(events), projections=list(projections), units=list(units)
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.apps.manager.get_app_manifest", lambda n: manifest if n == app else None
    )
    monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: enabled and n == app)
    grants.invalidate()
    return manifest


def _fake_config(agents, default=CREW):
    # memory_stores mirrors KiroCrewConfig: api_members reads it to mark a
    # member whose private store it owns (cfg.memory_stores.values()).
    return SimpleNamespace(agents=agents, default_agent=default, memory_stores={})


def _agent(**kw) -> KiroCrewAgentConfig:
    return KiroCrewAgentConfig(kiro_agent=kw.pop("kiro_agent", "reviewer"), **kw)


def _app(state, *, caller_app: str):
    """An aiohttp app serving the eventlog routes as *caller_app*."""
    from kiro_crew.dashboard.handlers.eventlog import (
        api_eventlog_events_get,
        api_eventlog_events_post,
        api_eventlog_projection_put,
        api_eventlog_projection_schema_put,
    )
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request, handler):
        request["app"] = caller_app
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/eventlog/{kind}/{id}/events", api_eventlog_events_get)
    app.router.add_post("/api/eventlog/{kind}/{id}/events", api_eventlog_events_post)
    app.router.add_post(
        "/api/eventlog/{kind}/{id}/projections/{key}/schema", api_eventlog_projection_schema_put
    )
    app.router.add_post("/api/eventlog/{kind}/{id}/projections/{key}", api_eventlog_projection_put)
    app.router.add_get("/api/members", api_members)
    return app


def _ensure_log():
    get_service().ensure(SLUG, CREW)


# ---------------------------------------------------------------------------
# §2 Manifest declaration
# ---------------------------------------------------------------------------
class TestManifestDeclaration:
    def test_round_trips_and_validates(self):
        raw = {
            "name": APP,
            "version": "1.0.0",
            "displayName": "Demo",
            "description": "d",
            "contributions": {
                "events": ["demoapp/ping"],
                "projections": ["demoapp/count"],
                "units": ["member"],
            },
        }
        m = AppManifest.from_dict(raw)
        assert m.contributions.units == ["member"]
        assert not [e for e in m.validate() if "contributions" in e]
        assert (
            AppManifest.from_dict(m.to_dict()).contributions.to_dict() == m.contributions.to_dict()
        )

    def test_a_foreign_namespace_is_refused(self):
        m = AppManifest.from_dict(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "D",
                "description": "d",
                "contributions": {"events": ["other/ping"], "units": ["member"]},
            }
        )
        errors = [e for e in m.validate() if "contributions.events" in e]
        assert errors and "must begin with 'demoapp/'" in errors[0]

    def test_the_bare_prefix_is_refused(self):
        m = AppManifest.from_dict(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "D",
                "description": "d",
                "contributions": {"projections": ["demoapp/"], "units": ["member"]},
            }
        )
        assert any("names the prefix and nothing else" in e for e in m.validate())

    def test_an_unknown_unit_kind_is_refused(self):
        m = AppManifest.from_dict(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "D",
                "description": "d",
                "contributions": {"events": ["demoapp/*"], "units": ["session"]},
            }
        )
        assert any("unknown unit kind 'session'" in e for e in m.validate())

    def test_patterns_without_units_are_refused(self):
        """A grant that reaches nothing reads as a broken app, not a manifest to fix."""
        m = AppManifest.from_dict(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "D",
                "description": "d",
                "contributions": {"events": ["demoapp/*"]},
            }
        )
        assert any("no units" in e for e in m.validate())

    def test_a_non_object_block_is_reported_not_erased(self):
        m = AppManifest.from_dict(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "D",
                "description": "d",
                "contributions": "yes",
            }
        )
        assert any("contributions must be an object" in e for e in m.validate())

    def test_a_non_array_list_is_reported(self):
        m = AppManifest.from_dict(
            {
                "name": APP,
                "version": "1.0.0",
                "displayName": "D",
                "description": "d",
                "contributions": {"events": "demoapp/*", "units": ["member"]},
            }
        )
        assert any("contributions.events must be an array" in e for e in m.validate())

    def test_declaration_is_covered_by_the_signature(self):
        base = {
            "name": APP,
            "version": "1.0.0",
            "displayName": "D",
            "description": "d",
            "signer": "s",
            "contributions": {"events": ["demoapp/a"], "units": ["member"]},
        }
        widened = dict(base, contributions={"events": ["demoapp/*"], "units": ["member"]})
        assert (
            AppManifest.from_dict(base).signing_payload()
            != AppManifest.from_dict(widened).signing_payload()
        )

    def test_an_undeclared_manifest_produces_the_pre_field_payload(self):
        """A manifest signed before this field existed must hash identically."""
        raw = {
            "name": APP,
            "version": "1.0.0",
            "displayName": "D",
            "description": "d",
            "signer": "s",
        }
        assert b"contributions" not in AppManifest.from_dict(raw).signing_payload()


# ---------------------------------------------------------------------------
# §2 Grants
# ---------------------------------------------------------------------------
class TestGrants:
    def test_prefixed_patterns_grant_and_others_do_not(self, monkeypatch):
        _grant(monkeypatch, events=("demoapp/ping",), projections=("demoapp/count",))
        assert grants.may_append(APP, "member", "demoapp/ping")
        assert not grants.may_append(APP, "member", "demoapp/other")
        assert grants.may_publish(APP, "member", "demoapp/count")
        assert not grants.may_publish(APP, "member", "demoapp/other")

    def test_a_pattern_naming_another_app_is_dropped_at_use(self, monkeypatch):
        """The manifest is a file an app can rewrite, so the prefix is re-checked."""
        _grant(monkeypatch, events=("victim/ping",))
        assert not grants.may_append(APP, "member", "victim/ping")

    def test_builtin_keys_can_never_be_published(self, monkeypatch):
        _grant(monkeypatch, projections=("demoapp/*",))
        for key in types.ALL_PROJECTION_KEYS:
            assert not grants.may_publish(APP, "member", key)

    def test_builtin_event_types_can_never_be_appended(self, monkeypatch):
        """The twin of the projection-key rule above, for the append side.

        Belt and braces, and honestly so: for an app named ``demoapp`` the prefix
        rule alone already refuses every built-in type, so this case passes with
        or without the guard. It is pinned for the same reason ``may_publish``'s
        built-in-key check is written explicitly rather than left to the prefix
        rule -- it is the contract's own sentence. The test that actually
        exercises the guard is the reserved-app-name one below, where the prefix
        rule matches and only the type check refuses.
        """
        _grant(monkeypatch, events=("demoapp/*",))
        for event_type in types.ALL_EVENT_TYPES:
            assert not grants.may_append(APP, "member", event_type), event_type

    def test_an_app_named_for_a_reserved_namespace_cannot_forge_builtin_events(self, monkeypatch):
        """The premise ``is_contributed_event_type`` documents but nothing enforced.

        Its contract reads "an app cannot be named for one of these", yet
        ``app_name_error`` reserves no namespace name. So an app installed as
        ``member`` declaring ``events: ["member/*"]`` passes
        ``Contributions.validate`` -- the prefix matches its own name and
        ``member`` is a known kind -- and its declaration then matches
        ``member/binding``, which is in ``ALL_EVENT_TYPES`` and which
        ``RosterProjection`` folds AUTHORITATIVELY. Without the type check the
        contributor overwrites gateway-owned roster fields it never owned, so the
        forgery is asserted by NAME here rather than left to the pattern rule.
        """
        forger = "member"
        _grant(monkeypatch, app=forger, events=("member/*",), projections=("member/*",))
        # The declaration itself is well-formed and the kind is granted: this is
        # authority being refused, not a malformed manifest being rejected.
        assert grants.may_use_kind(forger, "member")
        for event_type in ("member/binding", "member/created", "member/message"):
            if event_type in types.ALL_EVENT_TYPES:
                assert not grants.may_append(forger, "member", event_type), event_type
        # A genuinely contributed type under a non-reserved namespace is unaffected,
        # so the guard refuses the forgery rather than the protocol.
        _grant(monkeypatch, events=("demoapp/ping",))
        assert grants.may_append(APP, "member", "demoapp/ping")

    def test_an_ungranted_kind_denies_everything(self, monkeypatch):
        _grant(monkeypatch, units=())
        assert not grants.may_use_kind(APP, "member")
        assert not grants.may_append(APP, "member", "demoapp/ping")
        assert not grants.may_publish(APP, "member", "demoapp/count")

    def test_a_disabled_app_is_denied(self, monkeypatch):
        _grant(monkeypatch, enabled=False)
        assert not grants.declares_contributions(APP)
        assert not grants.may_append(APP, "member", "demoapp/ping")

    def test_matching_is_case_sensitive(self, monkeypatch):
        """An authority answer must not depend on the host filesystem's case rules."""
        _grant(monkeypatch, events=("demoapp/ping",))
        assert not grants.may_append(APP, "member", "demoapp/PING")


# ---------------------------------------------------------------------------
# §5 External projection store
# ---------------------------------------------------------------------------
class TestExternalProjectionStore:
    def test_higher_seq_wins_and_a_replay_is_refused(self):
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=1, seq=3, state_version=1)
        store.publish("member", SLUG, "demoapp/count", app=APP, value=2, seq=4, state_version=1)
        assert store.get("member", SLUG, "demoapp/count").value == 2
        with pytest.raises(ContribError) as exc:
            store.publish("member", SLUG, "demoapp/count", app=APP, value=9, seq=4, state_version=1)
        assert exc.value.code == "stale_seq" and exc.value.status == 409

    def test_a_higher_state_version_replaces_from_zero(self):
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=7, seq=50, state_version=1)
        store.publish("member", SLUG, "demoapp/count", app=APP, value=0, seq=0, state_version=2)
        row = store.get("member", SLUG, "demoapp/count")
        assert (row.value, row.seq, row.state_version) == (0, 0, 2)

    def test_an_older_state_version_is_refused(self):
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=1, seq=1, state_version=3)
        with pytest.raises(ContribError) as exc:
            store.publish(
                "member", SLUG, "demoapp/count", app=APP, value=2, seq=99, state_version=2
            )
        assert exc.value.code == "stale_seq"

    def test_rows_survive_a_restart(self, tmp_path):
        get_store().publish(
            "member", SLUG, "demoapp/count", app=APP, value=5, seq=1, state_version=1
        )
        # A brand-new store over the same root is the restart.
        reborn = ExternalProjectionStore(tmp_path / "eventlog" / "contrib")
        assert reborn.get("member", SLUG, "demoapp/count").value == 5

    def test_a_publish_whose_durable_write_fails_is_not_acknowledged(self, tmp_path, monkeypatch):
        """A publish that cannot persist must raise, not return success over a
        row that a cold reload would not find. The in-memory mutation is rolled
        back so the store's live view matches disk."""
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=1, seq=1, state_version=1)

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.atomic_write.atomic_write", boom)
        with pytest.raises(OSError):
            store.publish("member", SLUG, "demoapp/count", app=APP, value=2, seq=2, state_version=1)

        # Live view rolled back to the last durable value, not the failed one.
        assert store.get("member", SLUG, "demoapp/count").value == 1
        # A cold reload agrees: the failed publish never became durable.
        reborn = ExternalProjectionStore(tmp_path / "eventlog" / "contrib")
        assert reborn.get("member", SLUG, "demoapp/count").value == 1

    def test_a_schema_survives_a_value_publish(self):
        store = get_store()
        store.put_schema("member", SLUG, "demoapp/count", app=APP, schema={"kind": "badge"})
        store.publish("member", SLUG, "demoapp/count", app=APP, value=2, seq=1, state_version=1)
        assert store.get("member", SLUG, "demoapp/count").schema == {"kind": "badge"}

    def test_delete_app_rows_returns_what_it_removed_and_leaves_others(self):
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=1, seq=1, state_version=1)
        store.publish("member", SLUG, "other/x", app="other", value=1, seq=1, state_version=1)
        removed = store.delete_app_rows(APP)
        assert removed == [("member", SLUG, "demoapp/count")]
        assert store.get("member", SLUG, "demoapp/count") is None
        assert store.get("member", SLUG, "other/x") is not None

    def test_deleting_the_last_row_removes_the_file(self, tmp_path):
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=1, seq=1, state_version=1)
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        assert path.exists()
        store.delete_app_rows(APP)
        assert not path.exists()


# ---------------------------------------------------------------------------
# §4 Budget and size caps
# ---------------------------------------------------------------------------
class TestBudgetAndCaps:
    def test_over_budget_is_refused_not_queued(self):
        budget = contrib.EventBudget(limit=2)
        budget.charge(APP, "member", SLUG)
        budget.charge(APP, "member", SLUG)
        with pytest.raises(ContribError) as exc:
            budget.charge(APP, "member", SLUG)
        assert exc.value.code == "quota_exceeded" and exc.value.status == 429

    def test_the_budget_is_per_unit_and_per_app(self):
        budget = contrib.EventBudget(limit=1)
        budget.charge(APP, "member", SLUG)
        budget.charge(APP, "member", "other-slug")  # different unit
        budget.charge("other", "member", SLUG)  # different app
        with pytest.raises(ContribError):
            budget.charge(APP, "member", SLUG)

    def test_an_oversized_event_is_refused(self):
        with pytest.raises(ContribError) as exc:
            contrib.check_event_data({"blob": "x" * (contrib.MAX_EVENT_DATA_BYTES + 1)})
        assert exc.value.code == "event_too_large" and exc.value.status == 413

    def test_a_non_object_event_payload_is_refused(self):
        with pytest.raises(ContribError) as exc:
            contrib.check_event_data(["not", "an", "object"])
        assert exc.value.code == "invalid_projection_value"

    def test_an_unknown_schema_kind_is_refused(self):
        with pytest.raises(ContribError):
            contrib.normalize_schema({"kind": "iframe"})

    def test_schema_normalization_drops_unknown_fields(self):
        out = contrib.normalize_schema(
            {"kind": "table", "title": "T", "path": ["rows", "a"], "onClick": "alert(1)"}
        )
        assert out == {"kind": "table", "title": "T", "path": ["rows", "a"]}


# ---------------------------------------------------------------------------
# Event vocabulary: contributed types accepted, typo'd built-ins refused
# ---------------------------------------------------------------------------
class TestEventVocabulary:
    def test_a_namespaced_contributor_type_is_accepted(self):
        assert types.is_known_event_type("demoapp/ping")

    def test_a_typod_builtin_is_still_refused(self):
        assert not types.is_known_event_type("member/confg")
        assert not types.is_known_event_type("patrol/begun")

    def test_a_type_with_no_namespace_is_refused(self):
        assert not types.is_known_event_type("ping")
        assert not types.is_known_event_type("a/b/c")


# ---------------------------------------------------------------------------
# §3 catch-up read
# ---------------------------------------------------------------------------
class TestCatchUpRead:
    def test_events_after_is_oldest_first_and_exclusive(self):
        _ensure_log()
        svc = get_service()
        for i in range(5):
            svc.append(SLUG, "demoapp/ping", {"i": i})
        page = svc.events_after(SLUG, after=1, limit=10)
        seqs = [e["seq"] for e in page]
        # The two properties this test is named for, stated directly. A literal seq
        # list states neither: it also fails when the log legitimately holds an
        # event of its own, and it would still pass if the page came back reversed
        # as long as the numbers happened to line up.
        assert seqs == sorted(seqs), "oldest first"
        assert all(s > 1 for s in seqs), "after is exclusive"
        assert seqs[0] == 2, "no gap at the bound"
        assert seqs[-1] == svc.last_seq(SLUG), "the page runs to the end"

    def test_limit_bounds_the_page(self):
        _ensure_log()
        svc = get_service()
        for i in range(5):
            svc.append(SLUG, "demoapp/ping", {"i": i})
        page = svc.events_after(SLUG, after=-1, limit=2)
        # Bounded by the LIMIT, whatever the log holds. Anchoring on the first two
        # seqs would pass while measuring an event this test never wrote, so the
        # claim is the length.
        assert len(page) == 2

    @pytest.mark.asyncio
    async def test_route_returns_a_page_and_last_seq(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        svc = get_service()
        for i in range(3):
            svc.append(SLUG, "demoapp/ping", {"i": i})
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.get(f"/api/eventlog/member/{SLUG}/events?after=0&limit=10")
            status, body = res.status, await res.json()
        assert status == 200
        seqs = [e["seq"] for e in body["events"]]
        # A page that starts just past the bound, is in order, and ends at the seq
        # the body reports. Literals for the seqs and lastSeq say where these three
        # appends happened to land, which any other event in the log shifts, and say
        # nothing about the page agreeing with lastSeq.
        assert seqs == sorted(seqs) and all(s > 0 for s in seqs)
        assert seqs[0] == 1
        assert body["lastSeq"] == seqs[-1] == svc.last_seq(SLUG)
        assert body["slug"] == SLUG

    @pytest.mark.asyncio
    async def test_event_data_is_redacted_before_egress(self, tmp_path, monkeypatch):
        """The catch-up read serves raw envelopes from ``events_after``. An event's
        ``data`` can carry a credential or presigned URL, so it must pass the same
        redaction chain the member ``/history`` and ``/activity`` reads run before
        it crosses to a granted contributor."""
        import json

        _grant(monkeypatch)
        _ensure_log()
        svc = get_service()
        secret = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
        svc.append(SLUG, "demoapp/ping", {"note": secret})
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            body = await (
                await client.get(f"/api/eventlog/member/{SLUG}/events?after=-1&limit=10")
            ).json()
        blob = json.dumps(body)
        assert secret not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        # The event is still present (redacted), not dropped.
        assert any(e.get("type") == "demoapp/ping" for e in body["events"])

    @pytest.mark.asyncio
    async def test_bad_limit_and_after_carry_codes(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            r1 = await client.get(f"/api/eventlog/member/{SLUG}/events?limit=9999")
            r2 = await client.get(f"/api/eventlog/member/{SLUG}/events?after=abc")
            assert r1.status == 400 and (await r1.json())["code"] == "invalid_limit"
            assert r2.status == 400 and (await r2.json())["code"] == "invalid_after"


# ---------------------------------------------------------------------------
# §4 append route
# ---------------------------------------------------------------------------
class TestAppendRoute:
    @pytest.mark.asyncio
    async def test_a_granted_append_lands_with_a_server_assigned_seq(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        before = get_service().last_seq(SLUG)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": "demoapp/ping", "data": {"n": 1}, "seq": 999, "time": 1},
            )
            status, body = res.status, await res.json()
        assert status == 201
        # The contributor's seq/time are ignored; the gateway assigns both. Asserted
        # as "not the values the contributor sent" rather than as a literal seq: the
        # literal pins where this append happens to land in the fixture's log, which
        # any new built-in event shifts, and landing at a particular position is not
        # the property this test is named for.
        assert body["seq"] != 999 and body["time"] > 1
        # Exactly one event landed, and the seq the contributor was handed is that
        # one. A delta says both; an absolute value says neither.
        assert get_service().last_seq(SLUG) == before + 1 == body["seq"]

    @pytest.mark.asyncio
    async def test_an_undeclared_type_is_refused(self, tmp_path, monkeypatch):
        _grant(monkeypatch, events=("demoapp/ping",))
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        before = get_service().last_seq(SLUG)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/other", "data": {}}
            )
            status, body = res.status, await res.json()
        assert status == 403 and body["code"] == "event_type_not_owned"
        # Nothing was appended, asserted as the seq not MOVING rather than as an
        # empty log: emptiness also fails when the gateway legitimately holds an
        # event of its own, and this test is named for the refusal, not for the log
        # being bare.
        assert get_service().last_seq(SLUG) == before

    @pytest.mark.asyncio
    async def test_a_dashboard_user_is_refused(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app="")
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/ping", "data": {}}
            )
            status, body = res.status, await res.json()
        assert status == 403 and body["code"] == "unit_kind_not_granted"

    @pytest.mark.asyncio
    async def test_an_app_with_no_declaration_is_refused(self, tmp_path, monkeypatch):
        _grant(monkeypatch, events=(), projections=(), units=())
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/ping", "data": {}}
            )
            status, body = res.status, await res.json()
        assert status == 403 and body["code"] == "unit_kind_not_granted"

    @pytest.mark.asyncio
    async def test_an_unknown_unit_is_404(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                "/api/eventlog/member/no-such-member/events",
                json={"type": "demoapp/ping", "data": {}},
            )
            missing_status, missing_body = res.status, await res.json()
            kind = await client.post(
                f"/api/eventlog/session/{SLUG}/events", json={"type": "demoapp/ping", "data": {}}
            )
            kind_status, kind_body = kind.status, await kind.json()
        assert missing_status == 404 and missing_body["code"] == "unit_not_found"
        # An UNGRANTED kind answers on the grant, never on whether the unit is
        # there: see the no-oracle test below for why the two must not differ.
        assert kind_status == 403 and kind_body["code"] == "unit_kind_not_granted"

    @pytest.mark.asyncio
    async def test_an_ungranted_kind_cannot_be_used_to_probe_unit_existence(
        self, tmp_path, monkeypatch
    ):
        """The refusal must be identical for a unit that exists and one that does not.

        ``resolve_unit`` answers ``unit_not_found`` and the grant check answers
        ``unit_kind_not_granted``, so resolving the unit BEFORE authorizing the
        kind let an app with no grant on that kind walk IDs and read existence off
        the two different codes. Authorization runs first, so both answer alike.
        """
        _grant(monkeypatch, units=("member",))
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            present = await client.post(
                f"/api/eventlog/session/{SLUG}/events",
                json={"type": "demoapp/ping", "data": {}},
            )
            present_pair = (present.status, (await present.json())["code"])
            absent = await client.post(
                "/api/eventlog/session/no-such-unit-at-all/events",
                json={"type": "demoapp/ping", "data": {}},
            )
            absent_pair = (absent.status, (await absent.json())["code"])
        assert present_pair == absent_pair == (403, "unit_kind_not_granted")

    @pytest.mark.asyncio
    async def test_an_oversized_event_is_413(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": "demoapp/ping", "data": {"b": "x" * (64 * 1024 + 10)}},
            )
            status, body = res.status, await res.json()
        assert status == 413 and body["code"] == "event_too_large"

    @pytest.mark.asyncio
    async def test_over_budget_is_429_and_nothing_is_written(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        monkeypatch.setattr(contrib, "_budget", contrib.EventBudget(limit=1))
        app = _app(_make_state(tmp_path), caller_app=APP)
        before = get_service().last_seq(SLUG)
        async with TestClient(TestServer(app)) as client:
            ok = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/ping", "data": {}}
            )
            over = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/ping", "data": {}}
            )
            ok_status = ok.status
            over_status, over_body = over.status, await over.json()
        assert ok_status == 201
        assert over_status == 429 and over_body["code"] == "quota_exceeded"
        # Two requests, exactly ONE event: the refused one wrote nothing. A delta
        # says that; the absolute seq only says where the first one landed, which
        # any other event in the log shifts.
        assert get_service().last_seq(SLUG) == before + 1


# ---------------------------------------------------------------------------
# §5 publish route
# ---------------------------------------------------------------------------
class TestPublishRoute:
    @pytest.mark.asyncio
    async def test_publish_stores_and_pushes_the_kind_frame(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        state = _make_state(tmp_path)
        pushed: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda t, d: pushed.append((t, d))
        app = _app(state, caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount",
                json={"value": {"n": 3}, "seq": 2, "stateVersion": 1},
            )
            status = res.status
        assert status == 204
        assert get_store().get("member", SLUG, "demoapp/count").value == {"n": 3}
        # The EXISTING member_projection frame, so the page needs no new path.
        assert pushed == [
            (
                types.WS_MEMBER_PROJECTION,
                {"slug": SLUG, "key": "demoapp/count", "value": {"n": 3}, "seq": 2},
            )
        ]

    @pytest.mark.asyncio
    async def test_pushed_value_is_redacted(self, tmp_path, monkeypatch):
        """The live projection frame crosses to the browser the instant a value
        is published. A contributed value can carry a credential or presigned URL,
        so it must be scrubbed before broadcast -- otherwise it reaches the
        operator live-unredacted and is only redacted on the next page reload."""
        import json

        _grant(monkeypatch)
        _ensure_log()
        state = _make_state(tmp_path)
        pushed: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda t, d: pushed.append((t, d))
        app = _app(state, caller_app=APP)
        secret = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount",
                json={"value": {"note": secret}, "seq": 2, "stateVersion": 1},
            )
            assert res.status == 204
        blob = json.dumps(pushed)
        assert secret not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        # The frame is still sent (redacted), not dropped.
        assert pushed and pushed[0][1]["key"] == "demoapp/count"

    @pytest.mark.asyncio
    async def test_a_stale_publish_is_409(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount",
                json={"value": 1, "seq": 5, "stateVersion": 1},
            )
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount",
                json={"value": 2, "seq": 5, "stateVersion": 1},
            )
            status, body = res.status, await res.json()
        assert status == 409 and body["code"] == "stale_seq"

    @pytest.mark.asyncio
    async def test_a_builtin_key_cannot_be_published(self, tmp_path, monkeypatch):
        _grant(monkeypatch, projections=("demoapp/*",))
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/roster", json={"value": {}, "seq": 1}
            )
            status, body = res.status, await res.json()
        assert status == 403 and body["code"] == "projection_key_not_owned"

    @pytest.mark.asyncio
    async def test_another_apps_key_cannot_be_published(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/victim%2Fcount",
                json={"value": 1, "seq": 1},
            )
            status, body = res.status, await res.json()
        assert status == 403 and body["code"] == "projection_key_not_owned"

    @pytest.mark.asyncio
    async def test_a_missing_value_is_refused(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount", json={"seq": 1}
            )
            status, body = res.status, await res.json()
        assert status == 400 and body["code"] == "invalid_projection_value"

    @pytest.mark.asyncio
    async def test_schema_is_stored_and_repushed(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        state = _make_state(tmp_path)
        pushed: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda t, d: pushed.append((t, d))
        app = _app(state, caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount",
                json={"value": 4, "seq": 1, "stateVersion": 1},
            )
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2Fcount/schema",
                json={"kind": "badge", "title": "Pings"},
            )
            status = res.status
        assert status == 204
        assert get_store().get("member", SLUG, "demoapp/count").schema == {
            "kind": "badge",
            "title": "Pings",
        }
        assert pushed[-1][1]["schema"] == {"kind": "badge", "title": "Pings"}


# ---------------------------------------------------------------------------
# §5 contributed rows in the roster baseline
# ---------------------------------------------------------------------------
class TestRosterBaseline:
    @pytest.mark.asyncio
    async def test_contributed_rows_ride_the_projections_block_with_their_own_seq(
        self, tmp_path, monkeypatch
    ):
        _grant(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
            lambda: _fake_config({CREW: _agent(model="claude-x")}),
        )
        _ensure_log()
        svc = get_service()
        for i in range(4):
            svc.append(SLUG, "demoapp/ping", {"i": i})
        get_store().publish(
            "member", SLUG, "demoapp/count", app=APP, value={"n": 2}, seq=1, state_version=1
        )
        get_store().put_schema("member", SLUG, "demoapp/count", app=APP, schema={"kind": "badge"})

        app = _app(_make_state(tmp_path), caller_app="")
        async with TestClient(TestServer(app)) as client:
            body = await (await client.get("/api/members")).json()
        proj = body["members"][0]["projections"]
        # Same `values` map as the built-in keys -- no second client path.
        assert proj["values"]["demoapp/count"] == {"n": 2}
        assert set(types.ALL_PROJECTION_KEYS) <= set(proj["values"])
        # Its OWN seq, not the response's asOfSeq: seeding at asOfSeq would make
        # higher-seq-wins drop the contributor's next live push.
        assert proj["seqs"]["demoapp/count"] == 1
        assert proj["asOfSeq"] > 1
        assert proj["schemas"]["demoapp/count"] == {"kind": "badge"}

    @pytest.mark.asyncio
    async def test_a_schema_with_no_value_yet_renders_nothing(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
            lambda: _fake_config({CREW: _agent()}),
        )
        _ensure_log()
        get_store().put_schema("member", SLUG, "demoapp/count", app=APP, schema={"kind": "badge"})
        app = _app(_make_state(tmp_path), caller_app="")
        async with TestClient(TestServer(app)) as client:
            body = await (await client.get("/api/members")).json()
        assert "demoapp/count" not in body["members"][0]["projections"]["values"]


# ---------------------------------------------------------------------------
# §6 Teardown
# ---------------------------------------------------------------------------
class TestTeardown:
    @pytest.mark.asyncio
    async def test_disable_deletes_rows_pushes_null_and_keeps_events(self, monkeypatch):
        from kiro_crew.apps.teardown import teardown_contributions

        _grant(monkeypatch)
        _ensure_log()
        svc = get_service()
        svc.append(SLUG, "demoapp/ping", {"i": 0})
        pushed: list[tuple[str, dict]] = []
        svc.attach_broadcast(lambda t, d: pushed.append((t, d)))
        get_store().publish(
            "member", SLUG, "demoapp/count", app=APP, value=1, seq=0, state_version=1
        )
        events_before = svc.events_after(SLUG, after=-1, limit=10)
        seq_before = svc.last_seq(SLUG)

        warnings = await teardown_contributions(APP)

        assert warnings == []
        assert get_store().get("member", SLUG, "demoapp/count") is None
        assert pushed and pushed[-1][0] == types.WS_MEMBER_PROJECTION
        assert pushed[-1][1]["value"] is None and pushed[-1][1]["key"] == "demoapp/count"
        # Events STAY: they are history, and the log is never rewritten. Asserted as
        # the history being UNCHANGED across teardown, which is the claim, rather
        # than as a fixed seq and a fixed first element -- both of those move when
        # the log holds any other event, and neither says teardown left them alone.
        assert svc.last_seq(SLUG) == seq_before
        assert svc.events_after(SLUG, after=-1, limit=10) == events_before
        assert any(ev["type"] == "demoapp/ping" for ev in events_before)

    @pytest.mark.asyncio
    async def test_the_grant_is_invalidated_so_a_later_append_is_refused(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.apps.teardown import teardown_contributions

        _grant(monkeypatch)
        _ensure_log()
        assert grants.may_append(APP, "member", "demoapp/ping")
        # The app goes away; without the cache invalidation the grant would keep
        # answering yes for the rest of the TTL.
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: False)
        await teardown_contributions(APP)
        assert not grants.may_append(APP, "member", "demoapp/ping")

        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/ping", "data": {}}
            )
            status = res.status
        assert status == 403

    @pytest.mark.asyncio
    async def test_grant_stays_denied_while_still_enabled_during_teardown(
        self, tmp_path, monkeypatch
    ):
        """Fail-open window: `is_app_enabled` stays true until the config write
        later in the disable flow. A plain cache-invalidate would be re-populated
        with a live grant by any request in that window; the revoke tombstone must
        deny regardless of the still-true enabled state, and a re-enable lifts it."""
        from kiro_crew.apps.teardown import teardown_contributions
        from kiro_crew.eventlog.grants import unrevoke

        _grant(monkeypatch)
        _ensure_log()
        # App is STILL enabled -- simulate the teardown window before the config
        # write lands.
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True)
        assert grants.may_append(APP, "member", "demoapp/ping")
        await teardown_contributions(APP)
        # Even though the app still reads as enabled, the grant is denied.
        assert not grants.may_append(APP, "member", "demoapp/ping")
        # A re-enable lifts the tombstone so trust can be re-granted.
        unrevoke(APP)
        assert grants.may_append(APP, "member", "demoapp/ping")


# ---------------------------------------------------------------------------
# §2 path grant + §3 frame classification
# ---------------------------------------------------------------------------
class TestSurfaceGrants:
    def test_the_eventlog_prefix_is_granted_by_the_declaration_alone(self, monkeypatch):
        from kiro_crew.dashboard.token_auth import app_token_path_allowed

        _grant(monkeypatch)
        assert app_token_path_allowed(APP, f"/api/eventlog/member/{SLUG}/events")
        # And nothing else it did not declare.
        assert not app_token_path_allowed(APP, "/api/chat/slots")

    def test_an_app_with_no_declaration_does_not_get_the_prefix(self, monkeypatch):
        from kiro_crew.dashboard.token_auth import app_token_path_allowed

        _grant(monkeypatch, events=(), projections=(), units=())
        assert not app_token_path_allowed(APP, f"/api/eventlog/member/{SLUG}/events")

    def test_eventlog_frames_follow_the_declaration(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import ws_event_scope as wes

        state = _make_state(tmp_path)
        _grant(monkeypatch)
        assert wes.ws_event_allowed(
            "eventlog_event", {}, app=APP, allowed_events=frozenset(), state=state
        )
        _grant(monkeypatch, events=(), projections=(), units=())
        assert not wes.ws_event_allowed(
            "eventlog_event", {}, app=APP, allowed_events=frozenset(), state=state
        )

    def test_member_frames_stay_owner_only(self, tmp_path, monkeypatch):
        """A contribution grant must not open the operator's own crew frames."""
        from kiro_crew.dashboard import ws_event_scope as wes

        state = _make_state(tmp_path)
        _grant(monkeypatch)
        assert not wes.ws_event_allowed(
            types.WS_MEMBER_PROJECTION,
            {"slug": SLUG},
            app=APP,
            allowed_events=frozenset({"*"}),
            state=state,
        )


# ---------------------------------------------------------------------------
# §3 subscription hub
# ---------------------------------------------------------------------------
class TestSubscriptionHub:
    @pytest.mark.asyncio
    async def test_an_append_reaches_a_subscriber_in_order(self):
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        hub.start_pump(ws)
        for i in range(3):
            hub.publish("member", SLUG, {"type": "demoapp/ping", "seq": i, "time": 0, "data": {}})
        await ws.drain(3)
        seqs = [json.loads(m)["data"]["event"]["seq"] for m in ws.sent]
        assert seqs == [0, 1, 2]
        assert json.loads(ws.sent[0])["data"]["slug"] == SLUG

    @pytest.mark.asyncio
    async def test_a_published_event_is_redacted_before_fanout(self):
        """The live event frame crosses to a subscriber the instant an event is
        appended. Its ``data`` can carry a credential or presigned URL, so it must
        pass the same redaction chain the catch-up read and projection frames run
        before it is serialized and sent."""
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        hub.start_pump(ws)
        secret = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
        hub.publish(
            "member",
            SLUG,
            {"type": "demoapp/ping", "seq": 0, "time": 0, "data": {"note": secret}},
        )
        await ws.drain(1)
        assert secret not in ws.sent[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in ws.sent[0]
        # The event is still delivered (redacted), not dropped.
        assert json.loads(ws.sent[0])["data"]["event"]["type"] == "demoapp/ping"

    @pytest.mark.asyncio
    async def test_an_offloop_append_before_the_first_pump_is_not_lost(self):
        """F4 regression: an append that races the VERY FIRST subscribe — after
        subscribe registers the socket but before its pump starts, and arriving
        off the serving loop (the appending thread has no running loop) — must
        still reach the socket's queue. Before the fix, ``_serving_loop`` derived
        the loop only from an existing pump, found none on the first
        subscription, and dropped the fan-out."""
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)  # captures the serving loop
        # Publish from a worker thread (no running loop there) BEFORE start_pump.
        import asyncio

        await asyncio.to_thread(
            hub.publish,
            "member",
            SLUG,
            {"type": "demoapp/ping", "seq": 0, "time": 0, "data": {}},
        )
        # Now start the pump; the queued event drains out.
        hub.start_pump(ws)
        await ws.drain(1)
        assert [json.loads(m)["data"]["event"]["seq"] for m in ws.sent] == [0]

    @pytest.mark.asyncio
    async def test_an_unsubscribed_socket_receives_nothing(self):
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        hub.unsubscribe(ws, "member", SLUG)
        hub.start_pump(ws)
        hub.publish("member", SLUG, {"type": "demoapp/ping", "seq": 0, "time": 0, "data": {}})
        assert hub.subscriber_count("member", SLUG) == 0
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_a_slow_subscriber_is_closed_rather_than_buffered(self):
        """The contract's own remedy: closing forces a catch-up, buffering hides a gap."""
        from kiro_crew.dashboard import eventlog_ws

        hub = eventlog_ws.EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        # No pump: nothing drains the queue, so it fills.
        for i in range(eventlog_ws._QUEUE_LIMIT + 5):
            hub.publish("member", SLUG, {"type": "demoapp/ping", "seq": i, "time": 0, "data": {}})
        await _settle()
        assert ws.closed_code is not None
        assert hub.subscriber_count("member", SLUG) == 0

    @pytest.mark.asyncio
    async def test_drop_releases_the_subscription_and_the_pump(self):
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        hub.start_pump(ws)
        hub.drop(ws)
        assert hub.subscriptions(ws) == frozenset()
        assert hub.subscriber_count("member", SLUG) == 0

    @pytest.mark.asyncio
    async def test_close_app_closes_that_apps_sockets_only(self):
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        mine, theirs = _FakeWs(app=APP), _FakeWs(app="other")
        hub.subscribe(mine, "member", SLUG)
        hub.subscribe(theirs, "member", SLUG)
        assert await hub.close_app(APP) == 1
        assert mine.closed_code is not None and theirs.closed_code is None

    @pytest.mark.asyncio
    async def test_the_subscription_count_is_bounded(self):
        from kiro_crew.dashboard import eventlog_ws

        hub = eventlog_ws.EventLogHub()
        ws = _FakeWs(app=APP)
        for i in range(eventlog_ws._MAX_SUBSCRIPTIONS_PER_SOCKET):
            hub.subscribe(ws, "member", f"m{i}")
        with pytest.raises(eventlog_ws.SubscriptionLimit):
            hub.subscribe(ws, "member", "one-too-many")


async def _settle() -> None:
    """Let the loop run the hub's scheduled close tasks."""
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)


class _FakeWs:
    """The parts of a WebSocketResponse the hub touches."""

    def __init__(self, *, app: str) -> None:
        self._data = {"_app": app}
        self.sent: list[str] = []
        self.closed = False
        self.closed_code: int | None = None

    def get(self, key, default=None):
        return self._data.get(key, default)

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    async def close(self, *, code: int = 1000, message: bytes = b"") -> None:
        self.closed = True
        self.closed_code = code

    async def drain(self, n: int) -> None:
        import asyncio

        for _ in range(200):
            if len(self.sent) >= n:
                return
            await asyncio.sleep(0)
