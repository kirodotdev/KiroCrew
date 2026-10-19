"""The ``SessionLaneChanged`` emitter, exercised through the real HTTP routes.

Every other test of this event drives the dispatcher directly, which cannot catch a
wiring mistake at a call site: a handler that never calls it, calls it before the
persist, or hands it the wrong shape all still pass. These go through
``PUT /api/chat/slots/{slot}/tags`` and the drop route with a real
``TestClient``, so what they prove is that moving a session's lane through the API
fires the hook.

The shape matters and is the reason this file exists: ``slot.tags`` holds tag IDS,
while the ``status`` field that makes an id a LANE lives on the tag object in
``state._tags``. A dispatcher reading the ids as though they were tag dicts
classifies every one as not-a-lane and the event silently never fires -- which is
invisible to a test that passes dicts of its own making.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew import hooks as H
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.hooks import (
    HOOK_EVENT_SESSION_LANE_CHANGED,
    ScriptHookStore,
    set_global_hook_store,
)

_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret


@pytest.fixture(autouse=True)
def _reset_global_store(monkeypatch):
    """Unbind the process-global hook store, and permit the governance capability.

    Two host-independence concerns, both of which made this module pass for
    reasons unrelated to the code.

    The store: every test here binds one rooted at its own ``tmp_path``. Left
    bound, a later test on this worker that moves a status tag reaches the
    dispatcher, finds this store, and writes ``hooks.json`` back into a
    ``tmp_path`` pytest has already deleted -- a side effect outside the running
    test's own dir, arriving or not depending on test order.

    The capability: ``capabilities.script_hooks`` defaults to OFF, so with no
    config a hook is blocked before its subprocess spawns and writes nothing.
    A developer host that enables it makes every fire assertion here pass while
    the same test fails on a runner that has no config at all. The denial path
    is covered by its own test, which overrides this.
    """
    set_global_hook_store(None)  # type: ignore[arg-type]
    monkeypatch.setattr("kiro_crew.hooks._script_hooks_capability_denied", lambda sk="": None)
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


def _recording_store(tmp_path):
    """A store holding one enabled hook whose script appends its stdin payload."""
    out = tmp_path / "fired.jsonl"
    script = tmp_path / "lane_hook.sh"
    script.write_text(f"#!/bin/bash\ncat >> {out}\n", encoding="utf-8")
    script.chmod(0o755)
    # Built through the real write path, not by assigning `_hooks`. That
    # attribute is a dict keyed by id, so a list assigned to it makes every
    # reader iterate ID STRINGS -- which is exactly the production defect these
    # tests exist to catch, and a fixture that fakes the shape cannot see it.
    store = ScriptHookStore(tmp_path)
    store.create(
        {
            "name": "lane",
            "event": HOOK_EVENT_SESSION_LANE_CHANGED,
            "command": str(script),
            "enabled": True,
        }
    )
    set_global_hook_store(store)
    return store, out


async def _fired(out, *, timeout=30.0):
    """The payloads the hook recorded, once every dispatched task has finished.

    Awaits the dispatcher's own task set rather than sleeping. A fixed sleep is not
    a completion barrier in either direction: a loaded runner makes the positive
    assertions flake, and -- worse -- an absence assertion passes before the work it
    claims to exclude has even started, so a dispatcher that fired late would read
    as a dispatcher that correctly stayed silent.

    `_lane_dispatch_tasks` is the facade-bound set the dispatcher registers each
    spawned task in, so draining it is exactly "all dispatch for this call is
    done". Copied before awaiting because a task's done-callback discards itself
    from the live set.
    """
    import json

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        pending = [t for t in list(H._lane_dispatch_tasks) if not t.done()]
        if not pending:
            break
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"{len(pending)} lane dispatch task(s) still running")
        await asyncio.wait(pending, timeout=remaining)

    if not out.exists():
        return []
    return [json.loads(x) for x in out.read_text().splitlines() if x.strip()]


class TestTheRouteActuallyFiresTheEvent:
    @pytest.mark.asyncio
    async def test_moving_a_session_into_a_lane_fires_with_the_status_only_delta(
        self, tmp_path, monkeypatch
    ):
        """A lane change through PUT /tags fires once, carrying only status ids.

        The plain label moves in the same request and must NOT appear in the delta:
        emitting the whole set difference would put a non-status id into the matcher
        context, letting a hook match a tag the firing contract excludes.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            label = await (await client.post("/api/chat/tags", json={"name": "plain"})).json()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [done["id"], label["id"]]},
                )
            assert resp.status == 200

            rows = await _fired(out)
            assert len(rows) == 1, f"expected exactly one fire, got {rows}"
            assert rows[0]["hook_event_name"] == HOOK_EVENT_SESSION_LANE_CHANGED
            assert rows[0]["added"] == [done["id"]]
            assert rows[0]["removed"] == []
            assert label["id"] not in rows[0]["added"]
            assert store.list_all()[0].run_count == 1

    @pytest.mark.asyncio
    async def test_the_governance_capability_gate_blocks_the_fire(self, tmp_path, monkeypatch):
        """`capabilities.script_hooks` is honoured, and it is resolved from the
        FIRING session's key rather than from an empty one.

        The payload carries `session_key` so `run_script_hook` resolves the scope
        from that session's profile. Without it the scope resolves from `""`, which
        classifies as `unknown` rather than the session's own profile -- so a
        profile that disables script hooks would be bypassed by a board move.

        Asserted two ways, because a no-fire assertion alone passes for any reason
        at all: the hook does not run, AND the key the gate was asked about is the
        firing session's. The module's autouse fixture permits the capability, so
        this overrides it with a recording denial.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        asked: list[str] = []

        def _deny(sk: str = "") -> str:
            asked.append(sk)
            return "off"

        monkeypatch.setattr("kiro_crew.hooks._script_hooks_capability_denied", _deny)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [done["id"]]})
            assert resp.status == 200

            assert await _fired(out) == [], "a denied capability must run no command"
            # `dashboard:s1`, not a bare `s1`: `effective_session_key` qualifies the
            # key by surface, and the governance scope is resolved per surface. The
            # assertion pins the qualified form because that is what the profile
            # store is keyed by -- a bare id would resolve a different scope.
            assert asked == ["dashboard:s1"], f"gate asked about {asked}, not the firing session"
            assert store.list_all()[0].last_status == "blocked"

    @pytest.mark.asyncio
    async def test_a_plain_label_alone_does_not_fire(self, tmp_path, monkeypatch):
        """The control for the test above: without a status tag there is no lane
        change, so the same route must fire nothing. Without this, a dispatcher that
        fired on EVERY tag write would pass the positive test."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            label = await (await client.post("/api/chat/tags", json={"name": "plain"})).json()
            state._slots["s1"] = _ChatSlot("s1")
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [label["id"]]})
            assert resp.status == 200
            assert await _fired(out) == []

    @pytest.mark.asyncio
    async def test_a_refused_persist_fires_nothing(self, tmp_path, monkeypatch):
        """A rolled-back write never fires it.

        ``save_slot_off_loop`` returning False is the session-deleted-or-rebound
        path: the handler restores the prior tags and answers 409. An emitter placed
        before the persist, or on the rollback branch's own broadcast, would fire for
        a lane change that did not happen -- and for a close-out hook that means an
        irreversible action on a move the board rejected.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", return_value=False):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [done["id"]]})
            assert resp.status == 409
            assert await _fired(out) == []

    @pytest.mark.asyncio
    async def test_dragging_to_a_column_fires_both_directions(self, tmp_path, monkeypatch):
        """The drop route is the second emit site, and it strips as well as adds.

        Its mutual-exclusion rule removes every other status tag, so one drag is a
        two-direction transition. Both halves must reach the payload: a close-out
        hook keyed on ``*removed:<todo>;*`` is as legitimate a subscriber as one
        keyed on entering Done, and a plain label on the slot must survive the
        strip and stay out of the delta.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            todo = await (
                await client.post("/api/chat/tags", json={"name": "Todo", "status": True})
            ).json()
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            label = await (await client.post("/api/chat/tags", json={"name": "plain"})).json()
            slot = _ChatSlot("s1")
            slot.tags = [todo["id"], label["id"]]
            state._slots["s1"] = slot
            col = await (
                await client.post(
                    "/api/chat/tag-columns",
                    json={"tag_ids": [done["id"]], "mode": "any"},
                )
            ).json()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
            assert resp.status == 200

            rows = await _fired(out)
            assert len(rows) == 1, f"expected exactly one fire, got {rows}"
            assert rows[0]["added"] == [done["id"]]
            assert rows[0]["removed"] == [todo["id"]]
            assert label["id"] not in rows[0]["added"] + rows[0]["removed"]

    @pytest.mark.asyncio
    async def test_an_agent_caller_does_not_fire_the_operators_hook(self, tmp_path, monkeypatch):
        """A non-person caller moving a session must not run the operator's command.

        `chat_tag_assign` and `session_move_to_column` reach `PUT /tags` and `/drop`
        on an agent's behalf. An absent app claim does NOT make a caller the person:
        a plain agent session arrives over the internal-secret transport with no app
        claim at all, and so does an admitted crew-member principal. Keying the gate
        on the app claim alone lets an agent's board move execute the operator's
        shell hook, which is the case the gate exists to refuse.

        The verdict is read through the same shared enumeration the two routes use
        for their own refusals, so this asserts the dispatcher agrees with the
        handler rather than deciding identity a second way.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")

            class _AgentRequest(dict):
                def __init__(self) -> None:
                    super().__init__({"app": ""})
                    self.app = {"state": state}
                    self.headers = {"X-Internal-Secret": "x"}
                    self.query = {}

            permitted = H._lane_dispatch_is_permitted(_AgentRequest())[0]
            assert permitted is False, "an agent caller must not be permitted to fire"

            await H.dispatch_session_lane_changed_bulk(
                [("chat-agent", [], [done["id"]])],
                tags_by_id={done["id"]: done},
                session_keys={"chat-agent": "dashboard:s1"},
                request=_AgentRequest(),
            )
            assert await _fired(out) == []

    @pytest.mark.asyncio
    async def test_the_payload_carries_the_firing_sessions_key(self, tmp_path, monkeypatch):
        """`session_key` is what resolves the governance profile for the run.

        `run_script_hook` reads it to resolve the `capabilities.script_hooks` scope.
        Omitted, the scope resolves from an EMPTY key and is classified `unknown`
        rather than this session's profile, so a profile that disables script hooks
        is bypassed instead of enforced. The key has to be in the payload for the
        existing gate to do its job, which is why it is asserted here rather than
        left to that gate's own tests.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        _store, out = _recording_store(tmp_path)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [done["id"]]})
            assert resp.status == 200

            rows = await _fired(out)
            assert len(rows) == 1
            assert rows[0].get("session_key"), "the payload carries no session_key"
            assert rows[0]["session_key"].endswith("s1")

    @pytest.mark.asyncio
    async def test_no_registered_hook_means_no_dispatch_at_all(self, tmp_path, monkeypatch):
        """Zero subscribers is the default shipping state and must cost nothing.

        With no enabled hook the dispatcher returns before the permit gate and before
        the SEL row, so a host that never registers one adds no rows and spawns no
        tasks.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        store = ScriptHookStore(tmp_path)
        set_global_hook_store(store)
        async with TestClient(TestServer(app)) as client:
            done = await (
                await client.post("/api/chat/tags", json={"name": "Done", "status": True})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [done["id"]]})
            assert resp.status == 200
