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
import sys
from types import SimpleNamespace
from typing import Any
from unittest import mock

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

    def test_an_expired_entry_is_served_from_memory_and_refreshed_off_thread(self, monkeypatch):
        """These predicates are called from synchronous code on the gateway's event
        loop -- the per-frame event scoper and the auth path classifier -- so the
        manifest read behind an expired entry would stall every session on that
        loop, not just the request that asked, and would do so every TTL for the
        life of the process. An expired entry is therefore answered from memory and
        re-resolved on a worker thread. The assertion is on WHICH THREAD read the
        manifest, because the answer is the same either way.
        """
        import threading
        import time as _time

        _grant(monkeypatch, events=("demoapp/ping",))
        assert grants.may_append(APP, "member", "demoapp/ping")  # primes the entry

        read_on: list[int] = []
        real = grants._resolve

        def _watched(app: str):
            read_on.append(threading.get_ident())
            return real(app)

        monkeypatch.setattr(grants, "_resolve", _watched)
        # Expire the entry in place rather than sleeping out the TTL.
        with grants._cache_lock:
            grants._cache[APP] = (0.0, grants._cache[APP][1])

        caller = threading.get_ident()
        assert grants.may_append(APP, "member", "demoapp/ping"), "the stale answer was not served"
        assert caller not in read_on, "the manifest was read on the calling thread"

        # The refresh does land, so the entry does not go stale forever.
        for _ in range(200):
            if read_on:
                break
            _time.sleep(0.01)
        assert read_on, "no background refresh was started for the expired entry"


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

        # Faulted on the names ``pinned_fs`` itself calls, not on
        # ``kiro_crew.atomic_write``: that module's functions are bound into
        # ``pinned_fs``'s namespace by a module-level ``from ... import``, so
        # patching the source module leaves the already-bound name in place and the
        # publish simply succeeds. Both spellings are patched because the platform
        # chooses between them -- ``atomic_write_at`` on the pinned descriptor where
        # the no-follow walk is available, ``atomic_write`` on the fallback -- and a
        # pin that only faults one is a pin that stops measuring on the other.
        monkeypatch.setattr("kiro_crew.pinned_fs.atomic_write_at", boom)
        monkeypatch.setattr("kiro_crew.pinned_fs.atomic_write", boom)
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

    def test_a_hand_written_oversized_row_is_dropped_on_load(self, tmp_path):
        """The contrib root is not fenced from an agent's file tools, so a row can
        reach this file without passing ``publish``. Loading it unbounded retains
        it in memory AND ships it in every roster response for the unit, which is
        the unavailability ``MAX_PROJECTION_VALUE_BYTES`` exists to prevent -- and
        it does not self-heal, because a valid-JSON file reloads on every restart.
        """
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        over = "x" * (contrib.MAX_PROJECTION_VALUE_BYTES + 1)
        path.write_text(
            json.dumps(
                {
                    "demoapp/huge": {"value": over, "seq": 1, "stateVersion": 1, "app": APP},
                    "demoapp/ok": {"value": "small", "seq": 1, "stateVersion": 1, "app": APP},
                }
            ),
            encoding="utf-8",
        )
        store = get_store()
        assert store.get("member", SLUG, "demoapp/huge") is None
        # The bound drops the offending row only; a sibling within the cap loads.
        assert store.get("member", SLUG, "demoapp/ok").value == "small"

    def test_hand_written_rows_past_the_key_cap_are_dropped_on_load(self, tmp_path):
        """Same file, the other bound: the per-app key cap is charged at publish
        time, so a file carrying more keys than an app may hold bypasses it
        entirely unless the load path counts too."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = {
            f"{APP}/k{i}": {"value": i, "seq": 1, "stateVersion": 1, "app": APP}
            for i in range(contrib.MAX_PROJECTION_KEYS_PER_UNIT + 5)
        }
        path.write_text(json.dumps(rows), encoding="utf-8")
        store = get_store()
        held = [k for k in store.values("member", SLUG) if k.startswith(f"{APP}/")]
        assert len(held) == contrib.MAX_PROJECTION_KEYS_PER_UNIT

    def test_the_aggregate_is_bounded_by_the_file_ceiling_the_loader_applies(
        self, tmp_path, monkeypatch
    ):
        """The per-value and per-key bounds MULTIPLY past the file ceiling, so a run
        of individually legal publishes can build a file the loader then refuses.

        With the real constants a value may be 640 KiB and an app may hold 200 keys
        in one unit, while the loader refuses any file over 8 MiB -- so thirteen
        near-limit values, far inside the key bound, cross it. Each such publish
        reports success and the next cold load then starts empty, losing every row
        in the file rather than only the one that crossed. The ceiling is therefore
        charged at publish time against the same number the loader uses, and the
        refusal rolls back, so whatever is on disk always reloads whole.
        """
        monkeypatch.setattr(contrib, "MAX_STORE_FILE_BYTES", 4096)
        store = get_store()
        value = "v" * 512
        landed: list[str] = []
        refused: contrib.ContribError | None = None
        for i in range(contrib.MAX_PROJECTION_KEYS_PER_UNIT):
            try:
                store.publish(
                    "member",
                    SLUG,
                    f"{APP}/k{i}",
                    app=APP,
                    value=value,
                    seq=i + 1,
                    state_version=1,
                )
            except contrib.ContribError as exc:
                refused = exc
                break
            landed.append(f"{APP}/k{i}")
        assert refused is not None, (
            "the aggregate grew past the loader's file ceiling with no refusal, so the "
            "next cold load would drop every row in the file"
        )
        assert refused.code == "projection_store_too_large"
        # The file ceiling is what bound this, not the key cap: had the run gone all
        # the way to the cap, the pin would say nothing about the aggregate.
        assert 0 < len(landed) < contrib.MAX_PROJECTION_KEYS_PER_UNIT
        # The refused publish rolled back: the key it tried is absent, and every key
        # that did land is still held.
        assert store.get("member", SLUG, f"{APP}/k{len(landed)}") is None
        for key in landed:
            assert store.get("member", SLUG, key) is not None
        # The property that matters: a COLD load keeps them all, because the file was
        # never allowed past the size the loader refuses.
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        assert path.stat().st_size <= contrib.MAX_STORE_FILE_BYTES
        reloaded = get_store().values("member", SLUG)
        assert sorted(k for k in reloaded if k.startswith(f"{APP}/")) == sorted(landed)

    def test_a_rows_identity_fields_are_bounded_not_only_its_contents(self):
        """The caps above bound what a row CONTAINS; these bound what it IS.

        The key is a JSON object key in the unit's file and is shipped in every
        roster response, and the app name rides beside it, so capping the value
        and the key COUNT still leaves 200 slots whose NAMES can each hold
        megabytes. Refused rather than truncated: a shortened key can land on a
        key another app already holds, turning an over-long name into a silent
        overwrite of someone else's row.
        """
        store = get_store()
        over = "x" * (contrib.MAX_PROJECTION_IDENTITY_CHARS + 1)
        with pytest.raises(ContribError) as exc:
            store.publish("member", SLUG, f"{APP}/{over}", app=APP, value=1, seq=1, state_version=0)
        assert exc.value.code == "invalid_projection_key" and exc.value.status == 400
        with pytest.raises(ContribError):
            store.publish("member", SLUG, f"{APP}/k", app=over, value=1, seq=1, state_version=0)

    def test_the_schema_route_cannot_carry_an_unbounded_key_past_the_value_route(self):
        """``put_schema`` creates a value-less row, which is why the key-count cap
        is charged there too. The length bound needs the same treatment for the
        same reason, or the schema route is simply the way around it."""
        store = get_store()
        over = "x" * (contrib.MAX_PROJECTION_IDENTITY_CHARS + 1)
        with pytest.raises(ContribError) as exc:
            store.put_schema(
                "member", SLUG, f"{APP}/{over}", app=APP, schema={"kind": "text", "path": ["a"]}
            )
        assert exc.value.code == "invalid_projection_key"

    def test_a_hand_written_over_long_key_is_dropped_on_load(self, tmp_path):
        """Same file-not-fenced argument as the two tests above, for the identity
        fields: an over-long key written straight to the file is retained and
        shipped on every roster response, and it does not self-heal because the
        file is valid JSON and reloads on every restart."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        over = "x" * (contrib.MAX_PROJECTION_IDENTITY_CHARS + 1)
        path.write_text(
            json.dumps(
                {
                    f"{APP}/{over}": {"value": 1, "seq": 1, "stateVersion": 1, "app": APP},
                    f"{APP}/ok": {"value": "small", "seq": 1, "stateVersion": 1, "app": APP},
                }
            ),
            encoding="utf-8",
        )
        store = get_store()
        assert store.get("member", SLUG, f"{APP}/{over}") is None
        assert store.get("member", SLUG, f"{APP}/ok").value == "small"

    def test_fabricated_app_names_cannot_widen_the_row_cap_on_load(self, tmp_path):
        """The per-app cap is a real bound at PUBLISH time, where ``app`` comes from
        an authenticated token. On LOAD it is read from the file, so the WRITER picks
        how many buckets exist and a per-app cap is not an aggregate cap: each
        fabricated app name gets its own allowance. The file total is the ceiling a
        writer who chooses the bucket key cannot widen.
        """
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # One row per fabricated app, so EVERY per-app bucket holds exactly one and
        # the per-app cap is never reached however many rows there are.
        count = contrib.MAX_PROJECTION_KEYS_PER_FILE + 250
        rows = {
            f"app{i}/k": {"value": i, "seq": 1, "stateVersion": 1, "app": f"app{i}"}
            for i in range(count)
        }
        path.write_text(json.dumps(rows), encoding="utf-8")
        store = get_store()
        held = store.values("member", SLUG)
        assert len(held) <= contrib.MAX_PROJECTION_KEYS_PER_FILE, len(held)
        # Control: the per-app cap alone would have admitted every one of them.
        assert count > contrib.MAX_PROJECTION_KEYS_PER_FILE
        assert all(
            sum(1 for r in held.values() if r.app == f"app{i}")
            <= contrib.MAX_PROJECTION_KEYS_PER_UNIT
            for i in range(5)
        )

    def test_a_link_planted_at_the_store_name_is_refused_not_followed(self, tmp_path):
        """This root is agent-writable without going through ``publish``, so a name
        here resolves to whatever currently sits at it. A by-name read would print
        the link's target under the store's label, which makes the name a disclosure
        primitive; the read is pinned so a planted link is refused and the unit
        simply loads empty."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.json"
        outside.write_text(
            json.dumps(
                {"demoapp/stolen": {"value": "secret", "seq": 1, "stateVersion": 1, "app": APP}}
            ),
            encoding="utf-8",
        )
        path.symlink_to(outside)
        store = get_store()
        assert store.values("member", SLUG) == {}
        assert store.get("member", SLUG, "demoapp/stolen") is None

    def test_the_same_rows_at_a_regular_file_do_load(self, tmp_path):
        """Control for the planted-link refusal: identical bytes under the store's
        own name load normally, so that refusal is about what sits at the name and
        not about the content. A separate test because the loader caches per unit,
        so one test cannot read the same unit twice from disk."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"demoapp/stolen": {"value": "secret", "seq": 1, "stateVersion": 1, "app": APP}}
            ),
            encoding="utf-8",
        )
        assert get_store().get("member", SLUG, "demoapp/stolen").value == "secret"

    def test_a_credential_bearing_key_on_disk_is_dropped_on_load(self, tmp_path):
        """The key travels verbatim into the frame, the audit trail and this file,
        so the load path re-checks it for the same reason it re-checks the caps: a
        key planted by hand would otherwise reach every reader of the roster."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        leaky = f"{APP}/leak-" + "ghp_" + "a" * 36
        path.write_text(
            json.dumps(
                {
                    leaky: {"value": 1, "seq": 1, "stateVersion": 1, "app": APP},
                    f"{APP}/ok": {"value": 2, "seq": 1, "stateVersion": 1, "app": APP},
                }
            ),
            encoding="utf-8",
        )
        store = get_store()
        assert store.get("member", SLUG, leaky) is None
        # The offending row only; a sibling of identical shape still loads.
        assert store.get("member", SLUG, f"{APP}/ok").value == 2

    def test_a_planted_row_keyed_like_a_gateway_projection_is_dropped(self, tmp_path):
        """The roster response merges contributed rows OVER the gateway's own folds,
        so a row keyed like one of them answers in place of the value the gateway
        computed. No size bound can express that, and this file is writable without
        passing `publish`, so the publish-time refusal is not a bound on it."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    types.PROJ_ROSTER: {
                        "value": {"name": "forged"},
                        "seq": 1,
                        "stateVersion": 1,
                        "app": APP,
                    },
                    f"{APP}/ok": {"value": 2, "seq": 1, "stateVersion": 1, "app": APP},
                }
            ),
            encoding="utf-8",
        )
        store = get_store()
        assert store.get("member", SLUG, types.PROJ_ROSTER) is None
        # Scoped to the offending row: the app's own key still loads.
        assert store.get("member", SLUG, f"{APP}/ok").value == 2

    def test_a_planted_row_outside_its_apps_namespace_is_dropped(self, tmp_path):
        """The `<app>/` prefix is what ties a row to the contributor accountable for
        it, and teardown matches on `app` -- so a row claiming a prefix its own app
        does not own outlives the only app that could retract it."""
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "otherapp/borrowed": {
                        "value": 1,
                        "seq": 1,
                        "stateVersion": 1,
                        "app": APP,
                    },
                    f"{APP}/ok": {"value": 2, "seq": 1, "stateVersion": 1, "app": APP},
                }
            ),
            encoding="utf-8",
        )
        store = get_store()
        assert store.get("member", SLUG, "otherapp/borrowed") is None
        assert store.get("member", SLUG, f"{APP}/ok").value == 2


class TestProjectionAuthority:
    """The two authority rules and the dependency between them."""

    def test_every_gateway_key_is_a_bare_name(self):
        """Why the gateway-key rule cannot be reddened on its own: each of those keys
        carries no `/`, so the prefix rule refuses it first. This pins the property
        the docstring relies on -- a gateway key that ever carried a `/` would be
        admissible under the prefix rule to an app holding the matching name, and
        this test is what fails the day one is added."""
        assert [k for k in types.ALL_PROJECTION_KEYS if "/" in k] == []

    def test_every_gateway_key_is_refused(self):
        """The consequence, asserted directly rather than inferred. Either rule can
        be the one that fires, which is why this names neither: the two overlap on
        exactly this case, and only their absence together admits such a key."""
        for key in types.ALL_PROJECTION_KEYS:
            with pytest.raises(contrib.ContribError) as caught:
                contrib.check_projection_authority(key, APP)
            assert caught.value.code == "invalid_projection_key"

    def test_a_key_under_its_own_app_prefix_is_accepted(self):
        """CONTROL. The rules refuse impersonation, not ordinary publishing."""
        contrib.check_projection_authority(f"{APP}/count", APP)


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

    def test_a_publish_cannot_pin_a_key_with_an_unreachable_seq(self):
        """Higher wins and a Python int has no width, so one publish carrying 10**60
        would pin the key at a value no honest fold can advance past -- neither a
        refold nor an uninstall-and-republish takes it back. The ceiling is also
        what a browser can represent: these cross as JSON and land in a double.
        """
        assert contrib.MAX_PROJECTION_SEQ == 2**53 - 1
        store = get_store()
        store.publish(
            "member",
            SLUG,
            f"{APP}/k",
            app=APP,
            value=1,
            seq=contrib.MAX_PROJECTION_SEQ,
            state_version=0,
        )
        assert store.get("member", SLUG, f"{APP}/k").seq == contrib.MAX_PROJECTION_SEQ

    def test_the_publish_route_bounds_both_counters_above_not_only_below(self):
        """The store takes whatever number it is handed; the ROUTE is where a
        caller's value is validated, and it had a floor with no ceiling. Asserted on
        the source because both checks live inline in the handler rather than in a
        function a unit test can call.
        """
        import inspect

        from kiro_crew.dashboard.handlers import eventlog as handler

        src = inspect.getsource(handler)
        assert "check_projection_counters(raw_seq, raw_version)" in src, (
            "the route no longer bounds the counters above, so one publish pins the "
            "key at a value no honest fold can advance past"
        )

    def test_the_budget_is_per_unit_and_per_app(self):
        budget = contrib.EventBudget(limit=1)
        budget.charge(APP, "member", SLUG)
        budget.charge(APP, "member", "other-slug")  # different unit
        budget.charge("other", "member", SLUG)  # different app
        with pytest.raises(ContribError):
            budget.charge(APP, "member", SLUG)

    def test_an_app_cannot_mint_unbounded_projection_keys(self, monkeypatch):
        """A grant is a namespace PREFIX, so the key count needs its own bound.

        The event budget bounds a rate and resets daily; these rows are standing
        state, written to the unit's contrib file and shipped in every roster
        response, so one append that publishes one new key grows both without
        limit and the daily budget never reclaims it.
        """
        monkeypatch.setattr(contrib, "MAX_PROJECTION_KEYS_PER_UNIT", 2)
        store = contrib.get_store()
        for n in range(2):
            store.publish("member", SLUG, f"{APP}/k{n}", app=APP, value=n, seq=n, state_version=0)
        with pytest.raises(ContribError) as exc:
            store.publish("member", SLUG, f"{APP}/k2", app=APP, value=2, seq=5, state_version=0)
        assert exc.value.code == "quota_exceeded" and exc.value.status == 429
        # A key it already holds stays writable: the cap stops minting, not folding.
        store.publish("member", SLUG, f"{APP}/k0", app=APP, value=99, seq=10, state_version=0)
        assert store.values("member", SLUG)[f"{APP}/k0"].value == 99

    def test_a_retained_field_that_is_not_the_value_is_bounded_too(self):
        """A bound on ``value`` alone is not a bound on what a publish RETAINS.

        The schema is stored beside the value and re-sent with every frame, and a
        selector inside it is retained the same way. Capping only the selector
        COUNT left 32 slots that could each hold megabytes, which the value's own
        ceiling says nothing about.
        """
        # One selector far over its own ceiling: refused, not truncated, because a
        # shortened selector names a different path into the value.
        with pytest.raises(ContribError) as exc:
            contrib.normalize_schema({"kind": "text", "path": ["x" * 5000]})
        assert exc.value.code == "invalid_projection_value"
        assert "selector" in str(exc.value), str(exc.value)

        # A list OVER the selector cap is refused rather than sliced. This assertion
        # replaces one that fed 64 selectors and required exactly 32 to come back,
        # which pinned a silent DROP as the contract -- and a dropped selector makes
        # the card render a different shape than the contributor published, which is
        # the same objection the length check above already makes. Slicing also
        # capped the length check at the first 32, so a 33rd over-long selector was
        # never examined.
        with pytest.raises(ContribError) as exc:
            contrib.normalize_schema(
                {"kind": "table", "path": ["y" for _ in range(contrib.MAX_SCHEMA_SELECTORS + 1)]}
            )
        assert "selector limit" in str(exc.value), str(exc.value)

        # At the cap, the WHOLE schema is bounded by arithmetic rather than by a
        # second runtime check: the worst case a caller can get through is under the
        # derived ceiling.
        worst = {
            "kind": "table",
            "title": "t" * 400,
            "path": ["y" * contrib.MAX_SELECTOR_CHARS for _ in range(contrib.MAX_SCHEMA_SELECTORS)],
        }
        out = contrib.normalize_schema(worst)
        assert len(out["path"]) == contrib.MAX_SCHEMA_SELECTORS
        assert len(out["title"]) == 120, "title is still truncated"
        size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        assert size <= contrib.MAX_SCHEMA_BYTES, size

        # An ordinary schema still normalizes.
        assert contrib.normalize_schema({"kind": "badge", "path": ["a", "b"]}) == {
            "kind": "badge",
            "path": ["a", "b"],
        }

    def test_a_schema_title_is_redacted_before_it_is_truncated(self):
        """A credential straddling the 120-char title cut must not leak.

        Truncating first severs the key at character 120, and the redactors
        match a credential as a whole token, so the stump escapes them and the
        surviving prefix renders in the dashboard. The layout here puts the cut
        INSIDE the key, so a truncate-then-redact order goes red.
        """
        # Fake AWS access key id, matching (?:AKIA|ASIA)[A-Z0-9]{16}.
        token = "AKIA" + "CONTRIBSTRADDLE0"
        pad = "t" * 100 + " "
        title = f"{pad}{token} tail"
        start = len(pad)

        # Premise guard: the 120-char cut must fall INSIDE the key ...
        assert start < 120 < start + len(token)
        leaked = token[: 120 - start]
        # ... and a truncate-first order must really expose that prefix,
        # otherwise its absence below would prove nothing.
        assert leaked in title[:120]

        out = contrib.normalize_schema({"kind": "badge", "title": title})

        # The cap still binds, and no fragment of the key survives it.
        assert len(out["title"]) <= 120
        assert token not in out["title"]
        assert leaked not in out["title"], out["title"]
        assert "AKIA" not in out["title"], out["title"]

    def test_a_declared_grant_pattern_is_bounded_per_entry(self):
        """The pattern COUNT cap is only half a bound; each entry is retained too."""
        from kiro_crew.apps.manifest import Contributions

        decl = Contributions(
            units=["member"],
            events=[f"{APP}/" + "e" * 5000],
            projections=[],
        )
        errors = decl.validate(APP, frozenset({"member"}))
        assert any("characters, over the limit" in e for e in errors), errors

        ok = Contributions(units=["member"], events=[f"{APP}/thing"], projections=[])
        assert ok.validate(APP, frozenset({"member"})) == []

    def test_a_schema_publish_is_not_a_way_around_the_key_cap(self, monkeypatch):
        """``put_schema`` creates a value-less row, so it is charged the same."""
        monkeypatch.setattr(contrib, "MAX_PROJECTION_KEYS_PER_UNIT", 1)
        store = contrib.get_store()
        store.publish("member", SLUG, f"{APP}/only", app=APP, value=1, seq=0, state_version=0)
        with pytest.raises(ContribError) as exc:
            store.put_schema("member", SLUG, f"{APP}/extra", app=APP, schema={"kind": "badge"})
        assert exc.value.code == "quota_exceeded"

    def test_the_key_cap_is_per_app_so_one_contributor_cannot_spend_anothers(self, monkeypatch):
        monkeypatch.setattr(contrib, "MAX_PROJECTION_KEYS_PER_UNIT", 1)
        store = contrib.get_store()
        store.publish("member", SLUG, f"{APP}/mine", app=APP, value=1, seq=0, state_version=0)
        store.publish("member", SLUG, "other/theirs", app="other", value=1, seq=0, state_version=0)
        assert len(store.values("member", SLUG)) == 2

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
        assert [e["seq"] for e in page] == [2, 3, 4, 5]

    def test_limit_bounds_the_page(self):
        _ensure_log()
        svc = get_service()
        for i in range(5):
            svc.append(SLUG, "demoapp/ping", {"i": i})
        assert [e["seq"] for e in svc.events_after(SLUG, after=0, limit=2)] == [1, 2]

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
        assert [e["seq"] for e in body["events"]] == [1, 2, 3]
        assert body["lastSeq"] == 3
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
    async def test_an_event_type_is_bounded_before_it_is_written(self, tmp_path, monkeypatch):
        """The type is retained verbatim in every envelope, so it needs a ceiling.

        The namespace pattern a type must match constrains its SHAPE, not its
        length, so a grant-matching type of any size was written to the durable
        log once per append. Refused before the write, and the log stays empty.
        """
        _grant(monkeypatch)
        _ensure_log()
        svc = get_service()
        before = svc.last_seq(SLUG)
        long_type = f"{APP}/" + "z" * contrib.MAX_EVENT_TYPE_CHARS
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": long_type, "data": {"ok": 1}},
            )
            status, body = res.status, await res.json()
        assert status == 413, (status, body)
        assert body["code"] == "event_too_large", body
        assert svc.last_seq(SLUG) == before, "an over-long type reached the log"

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
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": "demoapp/ping", "data": {"n": 1}, "seq": 999, "time": 1},
            )
            status, body = res.status, await res.json()
        assert status == 201
        # The contributor's seq/time are ignored; the gateway assigns both.
        assert body["seq"] == 1 and body["time"] > 1
        assert get_service().last_seq(SLUG) == 1

    @pytest.mark.asyncio
    async def test_an_undeclared_type_is_refused(self, tmp_path, monkeypatch):
        _grant(monkeypatch, events=("demoapp/ping",))
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events", json={"type": "demoapp/other", "data": {}}
            )
            status, body = res.status, await res.json()
        assert status == 403 and body["code"] == "event_type_not_owned"
        assert get_service().last_seq(SLUG) == 0

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
        assert kind_status == 404 and kind_body["code"] == "unit_not_found"

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
    async def test_a_credential_shaped_event_type_is_refused_and_nothing_is_written(
        self, tmp_path, monkeypatch
    ):
        """The egress redactors scrub `data` only, and the type is stored verbatim,
        so a credential in the NAME segment would be permanent and would reach every
        co-subscribed app. It is refused at the boundary instead."""
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": f"{APP}/leak-" + "ghp_" + "a" * 36, "data": {}},
            )
            status, body = res.status, await res.json()
        assert status == 400 and body["code"] == "invalid_event_type", body
        assert get_service().last_seq(SLUG) == 0

    @pytest.mark.asyncio
    async def test_an_ordinary_type_of_the_same_shape_is_still_accepted(
        self, tmp_path, monkeypatch
    ):
        """The bound is on the name's CONTENT, so a type of identical shape and
        length carrying no credential goes through."""
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": f"{APP}/leak-" + "bbb_" + "a" * 36, "data": {}},
            )
            status = res.status
        assert status == 201
        assert get_service().last_seq(SLUG) == 1

    @pytest.mark.asyncio
    async def test_over_budget_is_429_and_nothing_is_written(self, tmp_path, monkeypatch):
        _grant(monkeypatch)
        _ensure_log()
        monkeypatch.setattr(contrib, "_budget", contrib.EventBudget(limit=1))
        app = _app(_make_state(tmp_path), caller_app=APP)
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
        assert get_service().last_seq(SLUG) == 1


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
        # `stateVersion` rides it because the browser's rule and this server's
        # rule are not the same rule: a publish whose stateVersion rose is
        # accepted here with a seq that did not advance, and a client applying
        # seq-wins alone would drop exactly that frame.
        assert pushed == [
            (
                types.WS_MEMBER_PROJECTION,
                {
                    "slug": SLUG,
                    "key": "demoapp/count",
                    "value": {"n": 3},
                    "seq": 2,
                    "stateVersion": 1,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_a_credential_shaped_key_is_refused_and_nothing_is_stored_or_pushed(
        self, tmp_path, monkeypatch
    ):
        """The frame carries the key verbatim beside the redacted value, and the key
        also lands in the SEL audit resources string and in the store file, so a
        credential in the name segment reaches every reader and no path takes it
        back. It is refused at the boundary, like the event type."""
        _grant(monkeypatch)
        _ensure_log()
        state = _make_state(tmp_path)
        pushed: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda t, d: pushed.append((t, d))
        app = _app(state, caller_app=APP)
        leaky = "leak-" + "ghp_" + "a" * 36
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2F{leaky}",
                json={"value": {"n": 3}, "seq": 2, "stateVersion": 1},
            )
            status, body = res.status, await res.json()
        assert status == 400 and body["code"] == "invalid_projection_key", body
        assert get_store().get("member", SLUG, f"demoapp/{leaky}") is None
        assert pushed == []

    @pytest.mark.asyncio
    async def test_an_ordinary_key_of_the_same_shape_is_still_accepted(self, tmp_path, monkeypatch):
        """The bound is on the name's CONTENT, so a key of identical shape and
        length carrying no credential publishes normally."""
        _grant(monkeypatch)
        _ensure_log()
        state = _make_state(tmp_path)
        app = _app(state, caller_app=APP)
        ordinary = "leak-" + "bbb_" + "a" * 36
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/projections/demoapp%2F{ordinary}",
                json={"value": {"n": 3}, "seq": 2, "stateVersion": 1},
            )
            status = res.status
        assert status == 204
        assert get_store().get("member", SLUG, f"demoapp/{ordinary}").value == {"n": 3}

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

        warnings = await teardown_contributions(APP)

        assert warnings == []
        assert get_store().get("member", SLUG, "demoapp/count") is None
        assert pushed and pushed[-1][0] == types.WS_MEMBER_PROJECTION
        assert pushed[-1][1]["value"] is None and pushed[-1][1]["key"] == "demoapp/count"
        # Events STAY: they are history, and the log is never rewritten.
        assert svc.last_seq(SLUG) == 1
        assert svc.events_after(SLUG, after=-1, limit=10)[0]["type"] == "demoapp/ping"

    def test_every_uninstall_caller_awaits_the_retraction(self):
        """Structural: the retraction belongs to the CALLER, and must be awaited there.

        ``forget_app_hooks`` clears the hook registries and is synchronous, so it
        cannot do this itself. Each async path that decides an app is gone has to
        await ``teardown_contributions`` before it reports success -- scheduled on
        the loop instead, a gateway exit in that window leaves the removed app's
        rows on disk, and nothing republishes them because the grant goes with the
        app. The uninstall route never runs ``teardown_app_runtime``, so its await
        is the ONLY retraction on that path, not a repeat of one.

        Structural because neither path has a test harness that drives it end to
        end, and a pin on the callee would say nothing now the work sits outside it.
        """
        from pathlib import Path

        from kiro_crew.apps import routes

        # ROUTES ONLY. The reconcile path awaits the retraction too, but not
        # adjacent to `forget_app_hooks`: it must run for a DISABLE as well, so it
        # sits OUTSIDE the `if gone:` block that forgets the hooks. Proximity is the
        # wrong invariant there, and a real behavioural test covers it --
        # `test_a_plain_disable_still_retracts_the_contributions` in
        # `test_hook_reconcile.py` fails if the retraction moves back under `gone`.
        # The uninstall route has no such harness, so it keeps this one.
        for module in (routes,):
            source = Path(module.__file__).read_text(encoding="utf-8")
            lines = source.splitlines()
            sites = [i for i, line in enumerate(lines) if "forget_app_hooks(" in line]
            sites = [i for i in sites if "import" not in lines[i]]
            assert sites, f"{module.__file__}: no call site found; this pin stopped measuring"
            for i in sites:
                window = "\n".join(lines[i : i + 12])
                assert "await teardown_contributions(" in window, (
                    f"{module.__file__}:{i + 1} forgets an app's hooks without awaiting "
                    "the contribution retraction, so the caller reports success over "
                    "rows still on disk"
                )
        # And the callee stays synchronous: making it async silently turns all of
        # its other callers into no-ops that return a coroutine.
        import inspect

        from kiro_crew.apps.teardown import forget_app_hooks

        assert not inspect.iscoroutinefunction(forget_app_hooks)


# ---------------------------------------------------------------------------
# The grant cache's first resolve must not run on the event loop
# ---------------------------------------------------------------------------
class TestTheFirstGrantResolveStaysOffTheEventLoop:
    """``_declaration`` resolves INLINE on a cache miss, and the resolve is real I/O.

    ``_resolve`` reads ``installed.json`` and the app manifest, neither of which the
    apps manager caches, so a cold lookup is two filesystem reads. Its synchronous
    consumers cannot become async -- the per-frame event scoper and the auth path
    classifier both answer inside a non-async function -- so the resolve is hoisted
    to the nearest async boundary and everything downstream of it is a cache hit.
    """

    @pytest.mark.asyncio
    async def test_warm_resolves_off_the_calling_thread(self, monkeypatch):
        import threading

        from kiro_crew.eventlog import grants as g

        _grant(monkeypatch)
        g.invalidate()
        threads: list[str] = []
        real_resolve = g._resolve

        def _recording(app):
            threads.append(threading.current_thread().name)
            return real_resolve(app)

        monkeypatch.setattr(g, "_resolve", _recording)
        here = threading.current_thread().name

        await g.warm(APP)

        assert threads, "warm did not resolve at all, so it cannot have warmed anything"
        assert here not in threads, (
            f"the resolve ran on the calling thread ({here}), which for the gateway is "
            "the event loop"
        )
        # And the synchronous gate behind it now needs no read of its own.
        spent = len(threads)
        assert g.declares_contributions(APP) is True
        assert len(threads) == spent, "the sync gate resolved again, so the warm bought nothing"

    @pytest.mark.asyncio
    async def test_warm_is_a_no_op_once_the_cache_is_warm(self, monkeypatch):
        # CONTROL. A warm that resolved unconditionally would satisfy the test above
        # and put a thread hop on every request instead of only the cold ones.
        from kiro_crew.eventlog import grants as g

        _grant(monkeypatch)
        g.invalidate()
        await g.warm(APP)
        assert g.is_cold(APP) is False
        calls: list[str] = []
        monkeypatch.setattr(g, "_resolve", lambda app: calls.append(app) or ((), (), ()))

        await g.warm(APP)

        assert calls == [], "warm resolved again although the cache was already warm"

    def test_no_app_scope_check_reads_the_declaration_before_a_warm(self):
        """Structural, because the behavioural pin above cannot reach the middleware.

        The scope gate is the FIRST thing to consult the declaration on a request, so
        a call site that enforces before warming puts the cold resolve back on the
        loop. Asserting the pairing at every site is what keeps a fourth one from
        being added without it.
        """
        from pathlib import Path

        from kiro_crew.dashboard import token_auth

        lines = Path(token_auth.__file__).read_text(encoding="utf-8").splitlines()
        sites = [i for i, line in enumerate(lines) if "= _enforce_app_scope(" in line]
        assert sites, "no scope enforcement site found; this pin has stopped measuring"
        for i in sites:
            preceding = lines[i - 1]
            assert "_warm_contribution_grant" in preceding, (
                f"{token_auth.__file__}:{i + 1} enforces app scope without warming the "
                f"contribution grant first, so the cold resolve runs on the event loop: "
                f"{preceding.strip()!r}"
            )

    @pytest.mark.asyncio
    async def test_a_delete_that_cannot_be_persisted_is_rolled_back_and_not_reported(
        self, monkeypatch
    ):
        """The in-memory delete is NOT the authoritative effect on this path.

        Teardown revokes the app's grant in the same flow, so no contributor is left
        to republish, and the next cold load restores the row from disk for good.
        Reporting the key as removed would broadcast the card as gone while the row
        survives, with nothing able to correct it -- so the unit is rolled back and
        left out of the answer, the shape ``publish`` and ``put_schema`` already use.
        """
        from kiro_crew.apps.teardown import teardown_contributions

        _grant(monkeypatch)
        _ensure_log()
        store = get_store()
        store.publish("member", SLUG, "demoapp/count", app=APP, value=1, seq=0, state_version=1)
        pushed: list[tuple[str, dict]] = []
        get_service().attach_broadcast(lambda t, d: pushed.append((t, d)))

        def refuse(*a, **k):
            raise OSError("disk is gone")

        monkeypatch.setattr("kiro_crew.pinned_fs.write_file_pinned", refuse)
        monkeypatch.setattr("kiro_crew.pinned_fs.unlink_pinned", refuse)

        warnings = await teardown_contributions(APP)

        assert warnings == [], warnings
        # Nothing told the dashboard the card was gone. Asserted FIRST because it is
        # the externally visible half: a client that received this frame renders no
        # card, and no later frame is coming to undo it.
        deletions = [d for t, d in pushed if d.get("value") is None]
        assert deletions == [], f"a deletion frame was pushed for a row that survives: {deletions}"
        # And the row is still held: a cold load would have restored it anyway, so
        # the in-memory map must agree with the disk rather than with the failed
        # intent.
        assert store.get("member", SLUG, "demoapp/count") is not None

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

    def test_contributions_are_revoked_before_the_apps_own_disable_code_runs(self):
        """``onDisable`` is the app's documented place to wind itself down, so it is
        the code most likely to make one last contribution call -- and it runs with
        the app's own token against the same HTTP surface. While the grant still
        answers yes, that write is accepted, and a write landing after the rows it
        folds into are deleted leaves a card no dashboard can explain.

        Asserted on SOURCE ORDER inside ``teardown_app_runtime`` rather than by
        driving a disable: reaching the script requires an installed app record, a
        live backend port probe and a real lifecycle script, none of which the
        ordering depends on. The two awaits are found by AST, so wrapping either in
        a condition does not hide it from this check.
        """
        import ast
        import inspect

        from kiro_crew.apps import teardown as teardown_mod

        tree = ast.parse(inspect.getsource(teardown_mod))
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "teardown_app_runtime"
        )
        first: dict[str, int] = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                if name in ("teardown_contributions", "run_lifecycle_script"):
                    first.setdefault(name, node.lineno)
                    first[name] = min(first[name], node.lineno)
        assert "teardown_contributions" in first, "the contributions teardown call is gone"
        assert "run_lifecycle_script" in first, "the onDisable script call is gone"
        assert first["teardown_contributions"] < first["run_lifecycle_script"], (
            "teardown_contributions runs at line "
            f"{first['teardown_contributions']}, after the app's onDisable script at "
            f"{first['run_lifecycle_script']}, so the app can still contribute "
            "while it is being disabled"
        )


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
class TestARevokedGrantCannotLandAnAppendOrASchema:
    """The route checks the grant, then hands the write off across an ``await``.

    An app revoked inside that window has already passed the check, so the answer the
    route holds is stale by the time the row lands. The publish path closes this by
    re-asking under the write lock via ``still_granted``; append and the schema write
    are the same window and now do the same thing.
    """

    def test_a_schema_write_is_refused_when_the_grant_is_gone(self, tmp_path):
        store = contrib.ExternalProjectionStore(tmp_path / "eventlog" / "contrib")

        with pytest.raises(contrib.ContribError) as exc:
            store.put_schema(
                "member",
                SLUG,
                f"{APP}/card",
                app=APP,
                schema={"title": "Card"},
                still_granted=lambda: False,
            )

        assert exc.value.code == "projection_key_not_owned"

    @pytest.mark.asyncio
    async def test_an_append_refused_under_the_lock_does_not_spend_quota(
        self, tmp_path, monkeypatch
    ):
        """The budget is charged BEFORE the write, so the write's own refusal has to
        hand it back.

        An app revoked inside the await window passes the route's check and is then
        refused under the write lock -- every append it has in flight, since the
        revocation refuses them all. Left charged, those refusals spend the app's
        whole day's budget on events the log never held, and it answers 429 once the
        grant returns.
        """
        _grant(monkeypatch)
        _ensure_log()
        svc = get_service()
        before = svc.last_seq(SLUG)
        # True for the route's pre-check, False for the recheck under the write
        # lock: the revocation lands inside the await, which is the window itself.
        asked = {"n": 0}

        def _granted_then_revoked(app_name, kind, event_type):
            asked["n"] += 1
            return asked["n"] == 1

        monkeypatch.setattr(grants, "may_append", _granted_then_revoked)
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": f"{APP}/ping", "data": {"ok": 1}},
            )
            status = res.status
        assert asked["n"] >= 2, "the recheck under the write lock never ran"
        assert status != 201, "a revoked append landed"
        assert svc.last_seq(SLUG) == before, "a refused append reached the log"
        assert contrib.get_budget().used(APP, "member", SLUG) == 0, (
            "the refused append kept its charge, so the app pays for an event the "
            "log does not hold"
        )

    @pytest.mark.asyncio
    async def test_an_append_that_lands_does_spend_quota(self, tmp_path, monkeypatch):
        # CONTROL. Releasing on every append would satisfy the test above and hand
        # the contributor an unmetered log.
        _grant(monkeypatch)
        _ensure_log()
        app = _app(_make_state(tmp_path), caller_app=APP)
        async with TestClient(TestServer(app)) as client:
            res = await client.post(
                f"/api/eventlog/member/{SLUG}/events",
                json={"type": f"{APP}/ping", "data": {"ok": 1}},
            )
            status = res.status
        assert status == 201, status
        assert contrib.get_budget().used(APP, "member", SLUG) == 1

    def test_a_schema_write_still_lands_while_the_grant_holds(self, tmp_path):
        # CONTROL. Refusing every schema would satisfy the test above and break the
        # feature the route exists to serve.
        store = contrib.ExternalProjectionStore(tmp_path / "eventlog" / "contrib")

        row = store.put_schema(
            "member",
            SLUG,
            f"{APP}/card",
            app=APP,
            schema={"title": "Card"},
            still_granted=lambda: True,
        )

        assert row.schema == {"title": "Card"}

    def test_the_recheck_runs_under_the_lock_not_before_it(self, tmp_path):
        # A recheck outside the lock is a recheck that can go stale, which is the
        # whole finding. Asserting the ORDER is what tells the two apart.
        store = contrib.ExternalProjectionStore(tmp_path / "eventlog" / "contrib")
        seen: list[str] = []
        real_lock = store._lock

        def _tracking_lock(kind, id_):
            seen.append("lock")
            return real_lock(kind, id_)

        store._lock = _tracking_lock  # type: ignore[method-assign]

        with pytest.raises(contrib.ContribError):
            store.put_schema(
                "member",
                SLUG,
                f"{APP}/card",
                app=APP,
                schema={"title": "Card"},
                still_granted=lambda: seen.append("grant") or False,
            )

        assert seen == ["lock", "grant"], f"the grant was asked outside the lock: {seen}"

    def test_an_append_is_refused_when_the_grant_is_gone(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as svc_mod

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure(SLUG, SLUG)

        with pytest.raises(PermissionError):
            svc.append(SLUG, "app/thing", {"a": 1}, still_granted=lambda: False)

    def test_an_append_still_lands_while_the_grant_holds(self, tmp_path, monkeypatch):
        # CONTROL, and it also covers the callers that pass no grant at all: the
        # gateway's own observers and the migration must stay unaffected.
        from kiro_crew.eventlog import service as svc_mod

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure(SLUG, SLUG)

        granted = svc.append(SLUG, "app/thing", {"a": 1}, still_granted=lambda: True)
        ungated = svc.append(SLUG, "app/thing", {"a": 2})

        assert granted["type"] == "app/thing"
        assert ungated["type"] == "app/thing"


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
    async def test_a_frame_whose_data_cannot_be_redacted_is_dropped_not_sent_raw(self):
        """The redaction arm must not fall back to the unredacted event.

        The redactor recurses, so a payload past the interpreter's limit makes it
        RAISE rather than return, and the old arm answered that by sending
        ``event`` itself -- the one control between agent-authored text and the
        browser failing OPEN on exactly the input that defeats it. Dropping is
        what the neighbouring ``json.dumps`` arm already does for the same reason.
        """
        import asyncio

        from kiro_crew.dashboard.eventlog_ws import EventLogHub
        from kiro_crew.eventlog import service as svc

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        hub.start_pump(ws)
        secret = "AKIAIOSFODNN7EXAMPLE"

        def exploding_redactor(_value):
            raise RecursionError("maximum recursion depth exceeded")

        with mock.patch.object(svc, "_redact_projection_value", exploding_redactor):
            hub.publish(
                "member",
                SLUG,
                {"type": "demoapp/ping", "seq": 0, "time": 0, "data": {"note": secret}},
            )
            await asyncio.sleep(0.05)
        assert ws.sent == [], f"an unredacted frame was sent: {ws.sent}"
        assert not any(secret in m for m in ws.sent)

    def test_the_serving_loop_lookup_does_not_walk_the_socket_map(self):
        """``publish`` calls ``_serving_loop`` from an APPENDING worker thread while
        a subscribe or drop rehashes ``_sockets`` on the serving loop, so walking
        that dict raised ``RuntimeError`` mid-iteration -- uncaught in ``publish``,
        escaping an ``EventSink`` this class documents as never raising, after the
        append had already landed. Asserted on the SOURCE because the race is timing
        dependent: a test that merely publishes would pass on a lucky interleaving.
        """
        import ast
        import inspect
        import textwrap

        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        body = inspect.getsource(EventLogHub._serving_loop)
        # Strip the docstring: it NAMES _sockets to explain the hazard, so a
        # substring check over the whole source matches the prose, not the code.
        fn = ast.parse(textwrap.dedent(body)).body[0]
        stripped = [
            n
            for n in fn.body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]
        src = "\n".join(ast.unparse(n) for n in stripped)
        assert "_sockets" not in src, (
            "_serving_loop reads _sockets again; publish calls it off-loop, so this "
            f"is the RuntimeError the hub already documents:\n{src}"
        )
        assert "_pump_loop" in src

    @pytest.mark.asyncio
    async def test_a_pumps_loop_is_recorded_for_the_appending_thread(self):
        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        assert hub._pump_loop is None, "no pump yet, so the fallback must be used"
        hub.start_pump(ws)
        assert hub._pump_loop is not None
        assert hub._serving_loop() is hub._pump_loop

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
    async def test_an_offloop_append_never_walks_the_loop_owned_peer_set(self):
        """The peer set belongs to the serving loop; only that loop may walk it.

        ``publish`` runs on whichever thread appended, so taking the subscriber
        snapshot there walks a set a concurrent subscribe or drop is rehashing on
        the serving loop, and CPython raises ``RuntimeError`` mid-walk. The
        caller's best-effort arm swallows it, so the append still lands while the
        subscriber stays open believing its fold is current -- a silent gap, not
        an error. The assertion is therefore on WHICH THREAD did the walking, not
        on delivery: delivery succeeds either way whenever the race does not fire.
        """
        import asyncio
        import threading

        from kiro_crew.dashboard.eventlog_ws import EventLogHub

        hub = EventLogHub()
        ws = _FakeWs(app=APP)
        hub.subscribe(ws, "member", SLUG)
        hub.start_pump(ws)

        serving_thread = threading.get_ident()
        walked: list[int] = []

        class _RecordingPeers(set):
            def __iter__(self):
                walked.append(threading.get_ident())
                return super().__iter__()

        key = ("member", SLUG)
        # Built from the live set, so this construction iterates the OLD set and
        # the recorder sees only the fan-out's own walk.
        hub._by_unit[key] = _RecordingPeers(hub._by_unit[key])

        await asyncio.to_thread(
            hub.publish,
            "member",
            SLUG,
            {"type": "demoapp/ping", "seq": 0, "time": 0, "data": {}},
        )
        await ws.drain(1)
        assert [json.loads(m)["data"]["event"]["seq"] for m in ws.sent] == [0]
        assert walked, "the peer set was never walked at all, so this proves nothing"
        assert set(walked) == {serving_thread}, (
            "the peer set was walked off the serving loop, on thread(s) "
            f"{set(walked) - {serving_thread}}"
        )

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


class TestAcceptedPayloadsCannotDefeatTheRedactor:
    """The redactor recurses, so DEPTH is a bound the byte caps do not provide:
    a thousand nested empty lists is about two kilobytes."""

    def test_event_data_nested_past_the_depth_bound_is_refused(self):
        deep: Any = {}
        node = deep
        for _ in range(contrib.MAX_JSON_DEPTH + 5):
            node["n"] = {}
            node = node["n"]
        with pytest.raises(ContribError) as exc:
            contrib.check_event_data(deep)
        assert "nests deeper" in str(exc.value), str(exc.value)

    def test_a_deep_payload_is_small_enough_to_pass_the_byte_cap(self):
        """States why depth needs its own bound: the size cap does not catch it."""
        deep: Any = []
        node = deep
        for _ in range(contrib.MAX_JSON_DEPTH + 5):
            child: list[Any] = []
            node.append(child)
            node = child
        size = len(json.dumps({"d": deep}).encode("utf-8"))
        assert size < contrib.MAX_EVENT_DATA_BYTES, size

    def test_a_published_value_nested_past_the_depth_bound_is_refused(self):
        """The projection broadcast runs the same redactor, so it needs the bound
        for the same reason the event path does."""
        deep: Any = {}
        node = deep
        for _ in range(contrib.MAX_JSON_DEPTH + 5):
            node["n"] = {}
            node = node["n"]
        with pytest.raises(ContribError):
            contrib.check_projection_value(deep)

    def test_the_depth_check_itself_does_not_recurse(self):
        """A recursive depth check would hit the very limit it guards and raise
        RecursionError instead of this module's coded refusal."""
        depth = sys.getrecursionlimit() * 3
        deep: Any = []
        node = deep
        for _ in range(depth):
            child: list[Any] = []
            node.append(child)
            node = child
        with pytest.raises(ContribError) as exc:
            contrib.check_json_depth(deep, "value")
        assert "nests deeper" in str(exc.value)


class TestLoadReChecksTheCountersToo:
    def test_a_hand_written_over_limit_seq_is_dropped_on_load(self, tmp_path):
        """Bounding the counters at the ROUTE is not bounding what LOAD retains, and
        the contrib file is writable without going through the route. HIGHER WINS, so
        a retained over-limit ``seq`` makes every valid publish for that key fail as
        stale from then on -- permanently, since no refold can reach the number.
        """
        path = tmp_path / "eventlog" / "contrib" / "member" / f"{SLUG}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    f"{APP}/pinned": {
                        "value": 1,
                        "seq": contrib.MAX_PROJECTION_SEQ + 1000,
                        "stateVersion": 1,
                        "app": APP,
                    },
                    f"{APP}/ok": {"value": "fine", "seq": 1, "stateVersion": 1, "app": APP},
                }
            ),
            encoding="utf-8",
        )
        store = get_store()
        assert store.get("member", SLUG, f"{APP}/pinned") is None
        assert store.get("member", SLUG, f"{APP}/ok").value == "fine"
        # And the key is publishable again, which is the harm the drop undoes.
        store.publish("member", SLUG, f"{APP}/pinned", app=APP, value=2, seq=5, state_version=0)
        assert store.get("member", SLUG, f"{APP}/pinned").value == 2

    def test_one_helper_bounds_the_counters_for_both_paths(self):
        """The route and the load path call the SAME function, so the two cannot
        drift apart -- which is how the load path came to be missing the check."""
        with pytest.raises(ContribError):
            contrib.check_projection_counters(contrib.MAX_PROJECTION_SEQ + 1, 0)
        with pytest.raises(ContribError):
            contrib.check_projection_counters(0, contrib.MAX_PROJECTION_SEQ + 1)
        contrib.check_projection_counters(contrib.MAX_PROJECTION_SEQ, 0)


class TestDeclaredUnitsAreBoundedLikeTheOtherLists:
    def test_too_many_declared_units_are_reported(self):
        """The validation loop covered events and projections and skipped units, so
        the count and length bounds never reached a list retained in the installed
        manifest exactly like its two siblings."""
        from kiro_crew.apps import manifest as manifest_mod
        from kiro_crew.apps.manifest import Contributions

        many = [f"kind{i}" for i in range(manifest_mod._MAX_CONTRIBUTION_PATTERNS + 5)]
        errors = Contributions(units=many, events=[], projections=[]).validate(APP, frozenset(many))
        assert any("contributions.units" in e and "exceeds" in e for e in errors), errors

    def test_an_over_long_unit_entry_is_reported(self):
        from kiro_crew.apps import manifest as manifest_mod
        from kiro_crew.apps.manifest import Contributions

        long_kind = "k" * (manifest_mod._MAX_CONTRIBUTION_PATTERN_CHARS + 1)
        errors = Contributions(units=[long_kind], events=[], projections=[]).validate(
            APP, frozenset({long_kind})
        )
        assert any("contributions.units" in e and "characters" in e for e in errors), errors

    def test_an_ordinary_declaration_still_validates(self):
        """Control: the new bound must not reject a normal one-kind declaration."""
        from kiro_crew.apps.manifest import Contributions

        ok = Contributions(units=["member"], events=[f"{APP}/thing"], projections=[])
        assert ok.validate(APP, frozenset({"member"})) == []
