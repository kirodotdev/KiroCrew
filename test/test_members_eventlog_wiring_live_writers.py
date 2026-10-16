"""Runtime writers into a member's event log, and the baseline a subscribing socket reads.

Covers the activity record and read round trip, the agent PUT config hook, the
slot events a member-created slot appends, and the ``members_subscribed``
baseline that holds and replays projections published while it is read.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_eventlog_wiring_helpers import _fresh_eventlog  # noqa: F401
from members_eventlog_wiring_helpers import CREW, _agent, _fake_config

from kiro_crew import eventlog_hooks, members
from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import get_service


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
        state.register_ws(ws, owner=True)
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
        state.register_ws(ws, owner=True)

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
