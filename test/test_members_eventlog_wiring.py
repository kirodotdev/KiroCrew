"""End-to-end wiring of the per-member append-only event log into the members
surfaces: the roster projections + lazy config reconcile, the startup
reconcile sweep, the record/read round trip, and the agent
PUT config-change hook.

Isolation: every test re-roots the members space at a fresh ``tmp_path`` by
monkeypatching ``kiro_crew.members.data_home`` (``get_service()`` rebuilds its
singleton when ``members_root()`` moves) and drops the cached service with
``set_service(None)`` via the ``_fresh_eventlog`` fixture, so no test observes
another test's log. The fixture pattern for the aiohttp routes mirrors the
neighbouring ``test_members_dm_thread.py``.
"""

from __future__ import annotations

import threading
import time
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
    from kiro_crew.dashboard.handlers.members import api_members

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
    async def test_two_rows_sharing_one_slug_are_both_blank(self, tmp_path, monkeypatch):
        """The projection map is keyed by slug, so a collision must blank BOTH rows.

        Two configured names can fold to one slug, and the response keys
        projections by that slug -- so whichever row is projected last owns the
        key. In one order the log's own member loses its state to the stranger's
        blank; in the other the stranger's row renders the owner's roster,
        activity, wake and driving state as its own. Neither is acceptable and the
        difference is iteration order, so a shared slug is blank for everyone.
        """
        other = "Code_Reviewer"
        assert members.slug_for_name(other) == members.slug_for_name(CREW), "precondition"
        cfg = _fake_config(
            {CREW: _agent(model="claude-x"), other: _agent(model="claude-y")},
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        app = _members_app(state)

        # Give the shared slug a log that genuinely belongs to ONE of them, with
        # state a wrong row would visibly render.
        svc = get_service()
        slug = members.slug_for_name(CREW)
        svc.ensure(slug, CREW)
        svc.append(slug, types.SLOT_OPENED, {"slot_key": "member-code-reviewer"})
        assert svc.last_seq(slug) >= 0

        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        rows = data["members"]
        assert len(rows) == 2, rows
        for row in rows:
            proj = row["projections"]
            assert proj["asOfSeq"] == -1, f"a colliding row was served a projection: {row}"
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
        it before the response crosses the network boundary -- the same chain the
        ``/activity`` read runs over the text it surfaces."""
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

    def test_an_explicit_member_id_decides_which_log_is_reconciled(self):
        """Reconcile by persisted identity, not by folding the display name.

        A member carrying an explicit ``member_id`` owns the log at that id. A
        sweep that folds the name instead creates and reconciles a SECOND log,
        so the member's history splits and their real log is never corrected.
        """
        # No space: the sweep skips any name failing _AGENT_NAME_RE, so a
        # two-word fixture would be skipped and the test would pass vacuously.
        name = "AliceExample"
        member_id = "alice-two"
        folded = members.slug_for_name(name)
        assert folded != member_id, "fixture must distinguish the two identities"

        cfg = _fake_config({name: _agent(member_id=member_id)}, default=name)
        svc = get_service()
        svc.ensure(member_id, name)
        before = svc.last_seq(member_id)

        eventlog_hooks.reconcile_members_at_startup(
            cfg, SimpleNamespace(_slots={}), SimpleNamespace(get_by_slot=lambda key: None)
        )

        assert svc.last_seq(member_id) > before, (
            "the sweep reconciled some other log: the member's own log at their "
            "explicit member_id gained nothing"
        )
        assert folded not in svc.slugs(), (
            f"the sweep folded the name and created a second log at {folded!r}, "
            "so this member's history is split across two files"
        )


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

    @pytest.mark.asyncio
    async def test_an_explicit_member_id_decides_which_log_the_event_lands_in(
        self, tmp_path, monkeypatch
    ):
        """A member may carry an explicit ``member_id``, and the roster keys their log by it.

        ``member_slug`` honours that id and falls back to the name fold; the bare fold
        does not. Writing an event under the fold while the roster reads the id is a
        split brain in which the member's own change never appears on their row, so the
        emit has to resolve through the same function the roster uses.
        """
        pinned = "pinned-log-id"
        agent = _agent(model="claude-x", starred=False)
        agent.member_id = pinned
        cfg = _fake_config({CREW: agent})
        assert members.slug_for_name(CREW) != pinned, "the fold must differ from the id"
        state = _make_state(tmp_path)
        svc = get_service()
        svc.ensure(pinned, CREW)
        seq_before = svc.last_seq(pinned)

        app = self._app(state, cfg, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            assert (await client.put(f"/api/agents/{CREW}", json={"starred": True})).status == 200

        assert svc.last_seq(pinned) == seq_before + 1, "the event did not land in the member's log"
        newest = svc.history(pinned, before=None, limit=1)[0]
        assert newest["type"] == types.MEMBER_CONFIG
        assert newest["data"]["starred"] is True


class TestBaselineSuppression:
    """A projection published while the baseline is computed must not be lost.

    ``send_members_subscribed`` reads ``last_seqs`` off the loop, and the socket is
    ALREADY in the owner broadcast set by then. An append landing in that window
    reaches the client ahead of a baseline computed before it, and the client's
    prune rule reads the newer row as stale and deletes it -- with no correction
    until that slug next changes.

    Reordering is not available: the connect snapshot must stay the first frame
    (``test_chat_send_echo_scope`` treats its arrival as proof of registration), so
    the window is closed by holding the frame back per socket and replaying the
    current value once the baseline is out.
    """

    class _WS:
        """Dashboard-user socket that records frames and carries per-socket state."""

        def __init__(self) -> None:
            self.closed = False
            self.sent: list[dict] = []
            self._flags: dict = {"_is_dashboard_user": True}

        def get(self, key, default=None):
            return self._flags.get(key, default)

        def __setitem__(self, key, value):
            self._flags[key] = value

        def pop(self, key, default=None):
            return self._flags.pop(key, default)

        async def send_str(self, msg: str) -> None:
            import json as _json

            self.sent.append(_json.loads(msg))

    @pytest.mark.asyncio
    async def test_a_projection_published_during_the_baseline_read_is_held_then_replayed(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        ws = self._WS()
        refused: list[bool] = []

        svc = get_service()

        def _slow_last_seqs():
            # Stands in for the real off-loop read: while it runs, the socket is
            # registered, so this is exactly when a publish reaches the fan-out.
            # The fan-out asks client_allowed per socket, so ask it the same way.
            allowed = state._ws_client_allowed(
                ws, types.WS_MEMBER_PROJECTION, {"slug": "alice", "key": types.PROJ_ROSTER}
            )
            refused.append(not allowed)
            return {"alice": 7}

        monkeypatch.setattr(svc, "last_seqs", _slow_last_seqs)
        monkeypatch.setattr(
            svc,
            "redacted_snapshot",
            lambda slug: {
                "asOfSeq": 7,
                "values": {types.PROJ_ROSTER: {"slug": slug, "name": slug}},
            },
        )

        await state.send_members_subscribed(ws)

        assert refused == [
            True
        ], "a projection reaching a socket without its baseline was delivered"
        kinds = [f["type"] for f in ws.sent]
        assert kinds[0] == "members_subscribed", "the baseline must go out first"
        assert types.WS_MEMBER_PROJECTION in kinds, "the held projection was never replayed"
        replay = [f for f in ws.sent if f["type"] == types.WS_MEMBER_PROJECTION]
        assert [f["data"]["slug"] for f in replay] == ["alice"]
        assert replay[0]["data"]["seq"] == 7, "the replay must carry the CURRENT seq"
        assert ws.get("_members_baseline_pending") is None, "the mark outlived the baseline"

    @pytest.mark.asyncio
    async def test_the_mark_is_released_when_the_baseline_read_fails(self, tmp_path, monkeypatch):
        """A socket left marked is suppressed for life, i.e. a blank Members page."""
        state = _make_state(tmp_path)
        ws = self._WS()

        def _boom():
            raise RuntimeError("log unreadable")

        monkeypatch.setattr(get_service(), "last_seqs", _boom)

        await state.send_members_subscribed(ws)

        assert ws.sent == []
        assert ws.get("_members_baseline_pending") is None, "a failed read left the socket muted"
        assert state._ws_client_allowed(
            ws, types.WS_MEMBER_PROJECTION, {"slug": "alice", "key": types.PROJ_ROSTER}
        ), "the socket stayed suppressed after the baseline failed"


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


class TestMemberCreatedSlotsReachTheMembersLog:
    """`_created_by` holds the creator's SLOT KEY, so comparing it against the
    agent-alias snapshot could not match for ANY member-created slot and every
    member slot event was dropped on the normal path. No existing test covered
    this: the sibling suites append to the service directly, which is why a guard
    that never fired still looked correct."""

    def test_a_dm_slot_creator_lands_its_slot_event_in_that_members_log(self, tmp_path):
        slug = members.slug_for_name(CREW)
        creator_key = members.DM_SLOT_KEY_PREFIX + slug
        st = _make_state(tmp_path)
        # Serialization is stubbed so the test drives the member-RESOLUTION block,
        # which is the finding; a real payload needs a full slot object and would
        # only add ways for this test to fail for an unrelated reason.
        st.serialize_slots = lambda *a, **k: []  # type: ignore[method-assign]
        st._slots = {
            creator_key: SimpleNamespace(key=creator_key, _created_by="", memory_store=""),
            "chat-7-1": SimpleNamespace(key="chat-7-1", _created_by=creator_key),
        }
        st._member_driven_slots_seen = {}

        svc = get_service()
        before = svc.last_seq(slug) if slug in set(svc.slugs()) else -1
        st._do_slots_broadcast()
        # The append is QUEUED on the ordered executor, so the broadcast
        # returning does not mean it reached the log. Drained with the same
        # function shutdown uses, which is the real guarantee -- sleeping here
        # would pass or fail on timing instead.
        assert eventlog_hooks.drain_for_shutdown(timeout=10), "the queued append never landed"
        after = svc.last_seq(slug)
        assert after > before, (
            "the member-created slot emitted nothing: a slot KEY was compared "
            f"against agent aliases (before={before} after={after})"
        )
        kinds = [e["type"] for e in svc.history(slug)]
        assert types.SLOT_OPENED in kinds, kinds

    def test_a_slot_key_is_not_an_agent_alias(self):
        """The premise, stated directly: this is why the old comparison could not
        match. A DM slot key carries the `member-` prefix; an alias never does."""
        slug = members.slug_for_name(CREW)
        key = members.DM_SLOT_KEY_PREFIX + slug
        assert members.slug_from_dm_slot_key(key) == slug
        assert key != slug, "a slot key is not the identity an alias map holds"


class TestMemberEventLogAppendsAreOrdered:
    """Two appends handed to the DEFAULT thread pool complete in either order.

    The member log is the authoritative record other surfaces read, so two
    transitions close together could be written newest-first with no crash and no
    unusual load involved. The single-worker executor lives in ``eventlog_hooks``,
    beside the ``emit`` every writer already calls, so a writer cannot offload an
    append without seeing it.
    """

    def test_the_shared_executor_has_exactly_one_worker(self):
        from kiro_crew import eventlog_hooks as hooks

        ex = hooks.io_executor()
        assert ex is hooks.io_executor(), "a fresh pool per call orders nothing"
        assert ex._max_workers == 1, (
            f"the pool has {ex._max_workers} workers, so two appends can still "
            "execute out of submission order"
        )

    def test_it_runs_submissions_in_submission_order(self):
        from kiro_crew import eventlog_hooks as hooks

        ex = hooks.io_executor()
        seen: list[int] = []

        def _job(n: int) -> None:
            # A later submission that finishes faster must still land later.
            time.sleep(0.02 if n == 0 else 0.0)
            seen.append(n)

        for f in [ex.submit(_job, n) for n in range(4)]:
            f.result(timeout=5)
        assert seen == [0, 1, 2, 3], f"appends completed out of order: {seen}"

    def test_no_offloaded_event_log_append_uses_the_default_pool(self):
        """Scans the WHOLE package, because the site this guard was first written
        for missed a third writer in another file entirely -- a per-module check
        cannot see that. Matched by what the offloaded function DOES (it calls
        ``eventlog_hooks.emit``) rather than by its name: a first attempt keyed on
        the name ``_emit`` and flagged session_map's SEL audit, which is a
        different subsystem and already retains and inspects its future.
        """
        import ast
        import pathlib

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        offenders: list[str] = []
        for f in sorted(root.rglob("*.py")):
            src = f.read_text(encoding="utf-8", errors="replace")
            if "eventlog_hooks.emit" not in src:
                continue
            tree = ast.parse(src)
            appenders = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "eventlog_hooks.emit" in (ast.get_source_segment(src, node) or "")
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_in_executor"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value is None
                    and isinstance(node.args[1], ast.Name)
                    and node.args[1].id in appenders
                ):
                    offenders.append(f"{f.relative_to(root)}:{node.lineno}")
        assert not offenders, (
            "these offload an event-log append to the default multi-worker pool, "
            f"so their order is luck: {offenders}"
        )

    def test_every_writer_reaches_the_executor_through_the_hooks_module(self):
        import pathlib

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        calls = sum(
            f.read_text(encoding="utf-8", errors="replace").count("eventlog_hooks.submit(")
            for f in root.rglob("*.py")
        )
        assert calls >= 3, f"expected the slot, message and patrol writers to share it, saw {calls}"


class TestQueuedAppendsSurviveShutdown:
    """A queued append is lost if the process exits before the worker runs, and
    the log is append-only with no replay, so the entry is simply gone. The
    shutdown window is ordinary operation rather than a crash.
    """

    def test_the_drain_waits_for_a_queued_append_to_finish(self):
        landed: list[str] = []

        def _slow_append() -> None:
            time.sleep(0.05)
            landed.append("written")

        eventlog_hooks.submit(_slow_append)
        assert eventlog_hooks.drain_for_shutdown(timeout=10) is True
        assert landed == ["written"], (
            "the drain returned before the queued append reached the log, so exit "
            "would report a complete record that is missing this entry"
        )

    def test_the_drain_reports_false_rather_than_hanging_on_a_wedged_append(self):
        """Bounded for the reason the crew-log drain documents: a wedged
        filesystem must delay exit, not hang it. False lets the caller report a
        short log instead of believing a complete one."""
        release = threading.Event()
        try:
            eventlog_hooks.submit(lambda: release.wait(30))
            assert eventlog_hooks.drain_for_shutdown(timeout=0.2) is False
        finally:
            release.set()
            eventlog_hooks.drain_for_shutdown(timeout=10)

    def test_submitting_registers_the_shutdown_drain(self):
        """The drain has to be ARMED by ordinary use: a hook that is only
        registered by an explicit setup call is a hook nobody calls."""
        eventlog_hooks.submit(lambda: None)
        eventlog_hooks.drain_for_shutdown(timeout=10)
        # The flag is the observable: atexit exposes no public list of callbacks.
        assert eventlog_hooks._drain_registered is True, (
            "submitting work did not arm the atexit drain, so a queued append "
            "would be lost at exit with nothing waiting for it"
        )

    def test_a_submit_failure_does_not_break_the_hooked_path(self, monkeypatch):
        """Writers call this from a best-effort hook, so a pool that cannot
        accept work must not propagate."""
        monkeypatch.setattr(
            eventlog_hooks, "io_executor", lambda: (_ for _ in ()).throw(RuntimeError("no pool"))
        )
        eventlog_hooks.submit(lambda: None)  # must not raise


class TestPatrolTransitionPersistsBeforeItPublishes:
    """The patrol frame must not be broadcast before its append has landed.

    The member log -- not the live loop -- is what the Crew Members drawer reads
    for a patrol's stop REASON across a restart:
    ``reconcile_members_at_startup`` closes a log that still reads ``armed`` with
    no live loop as ``reason='interrupted'``. So a transition published before its
    append landed lets a crash in that window replace the real reason
    (``runtime_budget``, say) permanently, with no later event able to correct it.

    Pinned at the CALL SITE rather than by driving the observer, because the
    observer is a closure built during gateway construction and reaching it needs
    the whole gateway stood up. The invariant that makes the ordering enforceable
    is that exactly ONE place performs the publish: once every caller goes through
    that one function, moving it relative to the append is a visible edit rather
    than an easy accident. Matched on the enclosing function of the call, not on a
    substring, so wrapping the call does not defeat it.
    """

    def _publish_call_owners(self) -> list[str]:
        """Names of the functions that broadcast an ``autonudge_state`` frame."""
        import ast
        import pathlib

        import kiro_crew

        src = (pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        owners: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call) or not inner.args:
                    continue
                first = inner.args[0]
                if isinstance(first, ast.Constant) and first.value == "autonudge_state":
                    # Attribute the call to the INNERMOST enclosing function: a
                    # nested def is walked by its parent too, so without this
                    # every ancestor would be reported as an owner.
                    deepest = node.name
                    for cand in ast.walk(node):
                        if (
                            isinstance(cand, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and cand is not node
                            and any(inner is c for c in ast.walk(cand))
                        ):
                            deepest = cand.name
                    owners.append(deepest)
        return owners

    def test_the_frame_is_published_from_exactly_one_function(self):
        owners = sorted(set(self._publish_call_owners()))
        assert owners == ["_publish"], (
            "an autonudge_state frame is broadcast from "
            f"{owners} rather than only from `_publish`; a second publish site is "
            "how the frame gets ahead of the append that makes it durable"
        )

    def test_the_observer_defers_the_publish_to_the_submitted_append(self):
        """The deferred path must exist AND be the one the append drives.

        Without this the test above passes on a `_publish` called straight from
        the observer, which is the pre-fix behaviour wearing the fixed shape.
        """
        import ast
        import pathlib

        import kiro_crew

        src = (pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        emit_fns = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_emit_patrol"
        ]
        assert emit_fns, "`_emit_patrol` is gone; the append is no longer offloaded"
        publishes_inside = [
            inner
            for fn in emit_fns
            for inner in ast.walk(fn)
            if isinstance(inner, ast.Call)
            and (
                (isinstance(inner.func, ast.Name) and inner.func.id == "_publish")
                or (
                    isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "call_soon_threadsafe"
                )
            )
        ]
        assert publishes_inside, (
            "`_emit_patrol` appends but never publishes, so the frame is published "
            "somewhere else -- which is the publish-before-persist ordering again"
        )

    def test_the_observers_own_publish_is_only_the_nothing_was_queued_fallback(self):
        """Closes the gap the two tests above leave open.

        Both of those still pass if the observer ALSO calls ``_publish()``
        unconditionally before submitting the append -- the frame stays funnelled
        through one function and the job still publishes, while the pre-fix
        ordering is fully restored. What rules that out is that the observer's own
        publish is reachable only when nothing was queued, i.e. it sits under an
        ``if``.
        """
        import ast
        import pathlib

        import kiro_crew

        src = (pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        observers = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "submit"
                and any(isinstance(a, ast.Name) and a.id == "_emit_patrol" for a in c.args)
                for c in ast.walk(n)
            )
        ]
        assert observers, "no function submits `_emit_patrol`; the append is not queued"

        unguarded: list[int] = []
        for obs in observers:
            # Statements of the observer itself, not of the nested `_emit_patrol`
            # (whose publish is the intended one) and not of `_publish` itself.
            nested = {
                id(x)
                for d in ast.walk(obs)
                if isinstance(d, ast.FunctionDef) and d.name in ("_emit_patrol", "_publish")
                for x in ast.walk(d)
            }
            for node in ast.walk(obs):
                if id(node) in nested or not isinstance(node, ast.Expr):
                    continue
                call = node.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_publish"
                ):
                    continue
                # The nearest enclosing `if` is not enough: this whole block sits
                # inside the observer's own `if loop is not None:` guard, so "is
                # inside SOME If" is true by construction and tests nothing. Only
                # an `if` whose TEST reads `_publish_deferred` makes the call the
                # nothing-was-queued fallback.
                guards = [
                    anc
                    for anc in ast.walk(obs)
                    if isinstance(anc, ast.If)
                    and any(
                        isinstance(t, ast.Name) and t.id == "_publish_deferred"
                        for t in ast.walk(anc.test)
                    )
                ]
                inside_if = any(any(node is s for s in ast.walk(g)) for g in guards)
                if not inside_if:
                    unguarded.append(node.lineno)
        assert not unguarded, (
            "the observer publishes the patrol frame unconditionally at "
            f"gateway.py line(s) {sorted(set(unguarded))}, so the frame is broadcast "
            "whether or not the append was queued -- publish-before-persist again"
        )

    def test_the_publish_is_gated_on_the_appends_answer_not_merely_ordered_after_it(self):
        """Ordering alone is not persistence.

        The three tests above pin that the publish happens inside the submitted
        append and not before it. They all still pass when `_emit_patrol` DISCARDS
        what `emit` answered and publishes regardless -- which is exactly the
        report-success-on-a-failed-write shape `persist-before-you-publish`
        forbids, because the frame then asserts a transition the ledger does not
        hold and the drawer reads the ledger for the stop reason after a restart.

        So: the result must be ASSIGNED (a bare expression statement is the
        return-not-read shape), and a guard on that name must be able to leave the
        function before any publish is reached.
        """
        import ast
        import pathlib

        import kiro_crew

        src = (pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        emit_fns = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_emit_patrol"
        ]
        assert emit_fns, "`_emit_patrol` is gone; the append is no longer offloaded"

        for fn in emit_fns:
            # The emit's answer is bound to a name rather than thrown away.
            bound: set[str] = set()
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                if any(
                    isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "emit"
                    for c in ast.walk(node.value)
                ):
                    bound.update(t.id for t in ast.walk(node) if isinstance(t, ast.Name))
            assert bound, (
                "`_emit_patrol` calls emit without binding its answer, so the "
                "publish below cannot depend on whether the append landed"
            )
            # A guard on one of those names returns early, so a failed append
            # reaches no publish.
            guarded_returns = [
                g
                for g in ast.walk(fn)
                if isinstance(g, ast.If)
                and any(isinstance(t, ast.Name) and t.id in bound for t in ast.walk(g.test))
                and any(isinstance(r, ast.Return) for r in ast.walk(g))
            ]
            assert guarded_returns, (
                "nothing in `_emit_patrol` leaves the function on a refused "
                f"append (names bound from emit: {sorted(bound)}), so the frame is "
                "published whether or not the ledger holds the transition"
            )

    def test_the_fallback_publish_is_not_reachable_on_a_refused_queue(self):
        """`_publish_deferred` answers "will a worker publish", not "was anything owed".

        Both are false when the queue REFUSES the append, and the fallback that owns
        the frame in that case published it anyway -- asserting a patrol transition the
        ledger never received. Gating `_emit_patrol` (the test above) does not close
        this: on a refusal `_emit_patrol` never runs at all.

        So the fallback's test must ALSO depend on whether a durable transition applied,
        not on `_publish_deferred` alone.
        """
        import ast
        import pathlib

        import kiro_crew

        src = (pathlib.Path(kiro_crew.__file__).parent / "slack" / "gateway.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)

        # The guard whose test reads `_publish_deferred` and which calls `_publish`.
        fallbacks = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            names = {t.id for t in ast.walk(node.test) if isinstance(t, ast.Name)}
            if "_publish_deferred" not in names:
                continue
            calls_publish = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_publish"
                for c in ast.walk(node)
            )
            if calls_publish:
                fallbacks.append((node, names))

        assert fallbacks, (
            "no guard on `_publish_deferred` publishes the frame any more; if the "
            "fallback moved, this pin must follow it rather than be deleted"
        )
        for node, names in fallbacks:
            assert len(names) > 1, (
                f"the fallback publish at line {node.lineno} is gated on "
                "`_publish_deferred` ALONE, so a refused queue -- which is also a "
                "false `_publish_deferred` -- publishes a transition the ledger "
                "never received"
            )
