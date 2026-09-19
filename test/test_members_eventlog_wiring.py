"""End-to-end wiring of the per-member append-only event log into the members
surfaces: the roster projections + lazy config reconcile, the raw history
route, the startup reconcile sweep, the record/read round trip, and the agent
PUT config-change hook.

Isolation: every test re-roots the members space at a fresh ``tmp_path`` by
monkeypatching ``kiro_crew.members.data_home`` (``get_service()`` rebuilds its
singleton when ``members_root()`` moves) and drops the cached service with
``set_service(None)`` via the ``_fresh_eventlog`` fixture, so no test observes
another test's log. The fixture pattern for the aiohttp routes mirrors the
neighbouring ``test_members_dm_thread.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import eventlog_hooks, members
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import get_service, set_service

CREW = "code-reviewer"


@pytest.fixture(autouse=True)
def _fresh_eventlog(tmp_path, monkeypatch):
    """Re-root the members space at tmp_path and drop any cached service.

    ``get_service()`` re-roots itself when ``members_root()`` (hence
    ``data_home()``) moves, but the singleton is also cleared explicitly so a
    prior test's in-memory logs can never answer here.
    """
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


def _fake_config(agents: dict[str, KiroCrewAgentConfig], default=CREW):
    # memory_stores mirrors KiroCrewConfig: the agent-update handler reads it
    # (cfg.memory_stores.get(...)) to resolve a member's private-memory record.
    return SimpleNamespace(agents=agents, default_agent=default, memory_stores={})


def _agent(**kw) -> KiroCrewAgentConfig:
    return KiroCrewAgentConfig(kiro_agent=kw.pop("kiro_agent", "reviewer"), **kw)


# ---------------------------------------------------------------------------
# 1. api_members: projections + idempotent lazy config reconcile
# ---------------------------------------------------------------------------
def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_member_history, api_members

    @web.middleware
    async def _auth(request, handler):
        # Same shape as test/dashboard_owner_helpers.py: `local-app` is the owner,
        # and a test that wants a NON-owner sends X-Test-User. Read from the header
        # so the owner gate below can actually be exercised on both sides -- a
        # fixture hard-coding the owner can only ever prove the owner still works,
        # which cannot tell a working gate from no gate at all.
        request["app"] = request.headers.get("X-Test-App", "")
        request["user"] = request.headers.get("X-Test-User", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/members/{slug}/history", api_member_history)
    return app


class TestApiMembersProjections:
    @pytest.mark.asyncio
    async def test_rows_carry_projections_and_reconcile_is_idempotent(self, tmp_path, monkeypatch):
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = data["members"][0]
        proj = row["projections"]
        assert set(proj["values"]) == {
            types.PROJ_ROSTER,
            types.PROJ_ACTIVITY,
            types.PROJ_WAKE,
            types.PROJ_DRIVING,
        }
        assert isinstance(proj["asOfSeq"], int)

        # First call's reconcile appended exactly one member/config (the log had
        # never seen one). A SECOND call must append nothing: the roster view
        # now matches the live config, so the reconcile is a no-op.
        svc = get_service()
        slug = members.slug_for_name(CREW)
        seq_after_first = svc.last_seq(slug)
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        assert svc.last_seq(slug) == seq_after_first, "config reconcile is not idempotent"

    @pytest.mark.asyncio
    async def test_a_row_whose_log_belongs_to_another_member_gets_no_projection(
        self, tmp_path, monkeypatch
    ):
        """One log holds ONE member's folded state, so a colliding row must not read it.

        A slug is lossy and colliding names are supported -- each activity entry
        keeps the exact name, which is what keeps attribution working. A whole-member
        PROJECTION cannot be shared that way: served on the wrong row it renders one
        member's roster, activity, wake and driving state as the other's. The row is
        served empty instead, and the header is what tells the two apart.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        # Give the slug a log that belongs to somebody else, then ask for the roster.
        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, "Somebody Else")
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-somebody-else"})
        assert svc.logged_name(slug) == "Somebody Else"

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        proj = data["members"][0]["projections"]
        assert proj["asOfSeq"] == -1, f"served another member's projection: {proj}"
        assert proj["values"] == {}

    @pytest.mark.asyncio
    async def test_a_header_locked_to_the_slug_still_serves_the_members_own_projection(
        self, tmp_path, monkeypatch
    ):
        """A placeholder header names nobody, so it is not evidence of a second member.

        ``ensure`` writes the header only while the log is fresh, so a writer with no
        name in hand -- the message path -- decides what that log claims for life. It
        resolves the name from the roster, but resolution can come up empty for a
        member the config does not carry yet, and then the header keeps the SLUG. A
        slug is a lossy fold, so reading that placeholder as an owner would blank the
        member's own state once they DO appear in the roster, on the strength of a
        value that was never a name.
        """
        named = "Code_Reviewer"
        slug = members.slug_for_name(named)
        assert slug != named, "this test needs a name its slug does not equal"

        # Phase 1: the member is not in the config, so the header keeps the slug.
        empty = _fake_config({})
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: empty
        )
        svc = get_service()
        svc.ensure(slug, slug)
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-code-reviewer"})
        assert svc.logged_name(slug) == slug, "resolution should have come up empty here"

        # Phase 2: the member now exists under a name the slug does not equal.
        cfg = _fake_config({named: _agent(model="claude-x")}, default=named)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        proj = data["members"][0]["projections"]
        assert proj["asOfSeq"] >= 0, f"own projection withheld on a placeholder header: {proj}"
        assert set(proj["values"]) == {
            types.PROJ_ROSTER,
            types.PROJ_ACTIVITY,
            types.PROJ_WAKE,
            types.PROJ_DRIVING,
        }

    @pytest.mark.asyncio
    async def test_a_fresh_log_resolves_a_placeholder_name_from_the_roster(
        self, tmp_path, monkeypatch
    ):
        """A nameless writer must not decide the header says the slug.

        ``emit`` passes ``name or slug``, so the message path reaches ``ensure`` with
        the slug. The header is written once, so accepting it would leave the log
        claiming a value that is not a name for life -- and the roster then has to
        treat it as unnamed, which costs the log its collision check. Resolution runs
        only on the fresh path, so a member's config is read once ever.
        """
        named = "Code_Reviewer"
        cfg = _fake_config({named: _agent(model="claude-x")}, default=named)
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        svc = get_service()
        slug = members.slug_for_name(named)
        assert slug != named, "this test needs a name its slug does not equal"

        svc.ensure(slug, slug)
        assert svc.logged_name(slug) == named, "the header kept the placeholder"

    @pytest.mark.asyncio
    async def test_projection_values_are_redacted_before_egress(self, tmp_path, monkeypatch):
        """The roster list embeds ``svc.snapshot()`` per member, and snapshot
        returns raw values. An activity record's ``project`` is operator-supplied
        and can embed a credential or presigned URL, so the list route must scrub
        it the same way the sibling ``/history`` read does -- otherwise the roster
        endpoint leaks what ``/history`` is careful to redact."""
        import json

        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        secret_url = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
        svc.append(
            slug,
            types.ACTIVITY_RECORD,
            {"ts": 1.0, "member": CREW, "project": secret_url, "via": "chat"},
        )

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get("/api/members")).json()
        blob = json.dumps(data)
        assert secret_url not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        # The projection block is still present (redacted), not dropped.
        assert data["members"][0]["projections"]["values"].get(types.PROJ_ACTIVITY) is not None

    @pytest.mark.asyncio
    async def test_editing_model_appends_one_member_config_changed_model(
        self, tmp_path, monkeypatch
    ):
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)
        slug = members.slug_for_name(CREW)

        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")
        svc = get_service()
        seq_before = svc.last_seq(slug)

        # Hand-edit the config's model, then hit the roster again: the reconcile
        # sees the drift and appends exactly one member/config with the single
        # changed field.
        cfg.agents[CREW].model = "gpt-y"
        async with TestClient(TestServer(app)) as client:
            await client.get("/api/members")

        assert svc.last_seq(slug) == seq_before + 1
        newest = svc.history(slug, before=None, limit=1)[0]
        assert newest["type"] == types.MEMBER_CONFIG
        assert newest["data"]["changed"] == ["model"]
        assert newest["data"]["model"] == "gpt-y"


# ---------------------------------------------------------------------------
# 2. history route: paging, ordering, limit validation with a machine code
# ---------------------------------------------------------------------------
class TestHistoryRoute:
    @pytest.mark.asyncio
    async def test_history_is_owner_only_and_the_owner_still_reads_it(self, tmp_path, monkeypatch):
        """Asserted on BOTH sides, which is the only way to tell a gate from no gate.

        The history read returns raw `member/rules` events -- the member's safety
        instructions -- and `_deny_app_caller` alone turns away app tokens while
        admitting any dashboard session. This PR already classifies the member
        projection frames as owner-only in ws_event_scope, so a REST read looser
        than the WS frame carrying the same state is the gap being closed.
        """
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)
        slug = members.slug_for_name(CREW)

        async with TestClient(TestServer(app)) as client:
            refused = await client.get(
                f"/api/members/{slug}/history", headers={"X-Test-User": "somebody-else"}
            )
            assert refused.status == 403, await refused.text()
            assert (await refused.json())["code"] == "owner_only"

            allowed = await client.get(f"/api/members/{slug}/history")
            assert allowed.status == 200, await allowed.text()

    @pytest.mark.asyncio
    async def test_newest_first_and_before_paging(self, tmp_path, monkeypatch):
        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        for i in range(5):
            svc.append(slug, types.MEMBER_MESSAGE, {"ts": float(i), "preview": str(i)})

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get(f"/api/members/{slug}/history")).json()
            assert [e["seq"] for e in data["events"]] == [5, 4, 3, 2, 1]
            assert data["lastSeq"] == 5
            paged = await (await client.get(f"/api/members/{slug}/history?before=3")).json()
        assert [e["seq"] for e in paged["events"]] == [2, 1]

    @pytest.mark.asyncio
    async def test_sensitive_event_data_is_redacted(self, tmp_path, monkeypatch):
        """The raw envelopes this route returns cross the same network boundary
        the sibling ``/activity`` route redacts. An activity record's ``project``
        is an operator-supplied path that can embed a credential or presigned
        URL; it must not reach the browser raw."""
        import json

        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        secret_url = "https://evil.example/x?token=AKIAIOSFODNN7EXAMPLE"
        svc.append(
            slug,
            types.ACTIVITY_RECORD,
            {"ts": 1.0, "member": CREW, "project": secret_url, "via": "chat"},
        )

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get(f"/api/members/{slug}/history")).json()
        # The whole serialized body must not carry the raw exfil URL / key.
        blob = json.dumps(data)
        assert secret_url not in blob
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        # The event is still present (redacted), not dropped.
        assert any(e.get("type") == types.ACTIVITY_RECORD for e in data["events"])

    @pytest.mark.asyncio
    async def test_a_credential_shaped_dict_key_is_redacted_too(self, tmp_path, monkeypatch):
        """A dict KEY crosses this boundary exactly as its value does.

        Every writer in this tree names its keys with code-owned structural words, so
        a credential-shaped key should be unproducible -- but that is a property of
        today's writers, not of this boundary, and one nested writer keying a dict by
        operator-supplied text would leak past a value-only pass. The redaction is
        pinned on both sides so the guarantee does not depend on auditing writers.
        """
        import json

        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(
            slug,
            types.ACTIVITY_RECORD,
            # Nested, because the recursion is what has to carry the rule down.
            {"ts": 1.0, "member": CREW, "changed": {"AKIAIOSFODNN7EXAMPLE": "rotated"}},
        )

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            data = await (await client.get(f"/api/members/{slug}/history")).json()
        blob = json.dumps(data)
        assert "AKIAIOSFODNN7EXAMPLE" not in blob, "a credential-shaped key reached the browser"
        # The entry is still there, redacted rather than dropped.
        assert any(e.get("type") == types.ACTIVITY_RECORD for e in data["events"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["0", "201"])
    async def test_out_of_range_limit_rejected_with_code(self, tmp_path, monkeypatch, bad):
        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/history?limit={bad}")
            assert resp.status == 400
            body = await resp.json()
        # The error-code contract test requires a machine-readable `code`.
        assert body["code"] == "invalid_limit"
        assert isinstance(body.get("error"), str) and body["error"]

    @pytest.mark.asyncio
    async def test_non_integer_limit_rejected_with_code(self, tmp_path, monkeypatch):
        cfg = _fake_config({CREW: _agent()})
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        slug = members.slug_for_name(CREW)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_members_app(state))) as client:
            resp = await client.get(f"/api/members/{slug}/history?limit=abc")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_limit"


# ---------------------------------------------------------------------------
# 3. reconcile_members_at_startup: synthesize interrupted closers, once
# ---------------------------------------------------------------------------
class TestStartupReconcile:
    def test_writes_one_closer_each_then_nothing_on_rerun(self):
        cfg = _fake_config({CREW: _agent()})
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        # Establish the config baseline first so the sweep's config-reconcile
        # step is a no-op — this test is about the two interrupted CLOSERS, not
        # the incidental first member/config.
        eventlog_hooks.reconcile_member_config(
            slug, CREW, cfg.agents[CREW], svc.snapshot(slug)["values"].get(types.PROJ_ROSTER, {})
        )
        # A wake armed for a slot the autonudge service does not hold, and a
        # driving.open slot missing from state._slots.
        svc.append(slug, types.PATROL_STARTED, {"slot_key": "member-code-reviewer"})
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "worker-1"})

        # autonudge has no loop for the armed slot; state holds neither slot.
        autonudge = SimpleNamespace(get_by_slot=lambda key: None)
        state = SimpleNamespace(_slots={})

        seq_before = svc.last_seq(slug)
        wrote = eventlog_hooks.reconcile_members_at_startup(cfg, state, autonudge)
        assert wrote == 2

        events = svc.history(slug, before=None, limit=10)
        newest_types = {(e["type"], e["data"].get("reason")) for e in events[:2]}
        assert (types.PATROL_STOPPED, "interrupted") in newest_types
        assert (types.SLOT_CLOSED, "interrupted") in newest_types
        assert svc.last_seq(slug) == seq_before + 2, "only the two closers should be appended"

        # A second run appends nothing: patrol is now stopped, the slot closed,
        # and the config still matches.
        seq_after = svc.last_seq(slug)
        assert eventlog_hooks.reconcile_members_at_startup(cfg, state, autonudge) == 0
        assert svc.last_seq(slug) == seq_after


# ---------------------------------------------------------------------------
# 4. record_activity -> read_activity round trip + dedupe
# ---------------------------------------------------------------------------
class TestActivityRoundTrip:
    def test_round_trip(self):
        assert members.record_activity(CREW, "s1", "persistent", project="/repo", via="chat")
        assert members.record_activity(CREW, "s2", "persistent", via="chat")
        rows = members.read_activity(members.slug_for_name(CREW))
        assert [r["session"] for r in rows] == ["s1", "s2"]
        assert rows[0]["project"] == "/repo"
        assert rows[0]["member"] == CREW

    def test_dedupe_session(self):
        assert members.record_activity(CREW, "s1", "persistent", via="chat", dedupe_session=True)
        assert (
            members.record_activity(CREW, "s1", "persistent", via="chat", dedupe_session=True)
            is False
        )
        assert len(members.read_activity(members.slug_for_name(CREW))) == 1


# ---------------------------------------------------------------------------
# 5. PUT /api/agents/{name}: member/config only on a real roster-field change
# ---------------------------------------------------------------------------
class TestAgentPutConfigHook:
    def _app(self, state, cfg, monkeypatch):
        from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_update

        # api_kirocrew_agent_update reloads and SAVES the config; patch both the
        # module-level loader and the instance's save so the PUT stays in-memory.
        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", lambda: cfg)
        cfg.save = lambda: None
        # main routes the write through memory_stores.persist_member_config, which
        # reloads config from disk under a lock; stub it so the PUT stays in-memory
        # (this fake cfg is not persisted) and the eventlog hook still fires.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.persist_member_config",
            lambda *a, **k: None,
        )

        @web.middleware
        async def _auth(request, handler):
            request.setdefault("app", "")
            request.setdefault("user", "local-app")
            return await handler(request)

        app = web.Application(middlewares=[_auth])
        app["state"] = state
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        return app

    @pytest.mark.asyncio
    async def test_no_roster_field_change_appends_nothing(self, tmp_path, monkeypatch):
        cfg = _fake_config({CREW: _agent(model="claude-x")})
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        seq_before = svc.last_seq(slug)

        app = self._app(state, cfg, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            # description is not one of the 7 config-derived roster fields.
            resp = await client.put(f"/api/agents/{CREW}", json={"description": "hi"})
            assert resp.status == 200
        assert svc.last_seq(slug) == seq_before, "a non-roster edit must append no member/config"

    @pytest.mark.asyncio
    async def test_flipping_starred_appends_one_member_config(self, tmp_path, monkeypatch):
        cfg = _fake_config({CREW: _agent(model="claude-x", starred=False)})
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        seq_before = svc.last_seq(slug)

        app = self._app(state, cfg, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/agents/{CREW}", json={"starred": True})
            assert resp.status == 200

        assert svc.last_seq(slug) == seq_before + 1
        newest = svc.history(slug, before=None, limit=1)[0]
        assert newest["type"] == types.MEMBER_CONFIG
        assert newest["data"]["changed"] == ["starred"]
        assert newest["data"]["starred"] is True


def test_the_connect_baseline_call_site_exists_in_the_ws_handler():
    """The hub method is useless without a caller, and A shipped it without one.

    ``send_members_subscribed`` builds the one-shot ``members_subscribed`` frame
    the client needs to prune held projections against an authoritative
    ``lastSeqs``. The method lived in websocket_hub.py while its only call site
    lived in ws.py, so extracting one without the other left the frame documented
    and never sent -- the client kept rows the server had rolled back. Asserted on
    the source because the call sits inside the connect path of a socket handler
    that a unit test cannot drive without standing up a live WebSocket.
    """
    from pathlib import Path as _Path

    ws = _Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/ws.py"
    source = ws.read_text(encoding="utf-8")
    assert (
        "await state.send_members_subscribed(ws)" in source
    ), "the connect path must send the members_subscribed baseline"
    hub = _Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/websocket_hub.py"
    assert "def send_members_subscribed" in hub.read_text(encoding="utf-8")

    # The baseline is sent AFTER the connect snapshot, and that position is not
    # free to change: test_chat_send_echo_scope reads the first frame on each
    # connection and requires it to be `slots`, using its arrival as proof the
    # socket is registered for echoes. Moving the baseline ahead of register_ws
    # makes it the first frame and fails that contract on four shards plus E2E.
    baseline_at = source.index("await state.send_members_subscribed(ws)")
    snapshot_at = source.index("await ws.send_str(snapshot_payload)")
    assert snapshot_at < baseline_at, (
        "the members_subscribed baseline precedes the slots connect snapshot, which "
        "test_chat_send_echo_scope requires to be the first frame on the socket"
    )
