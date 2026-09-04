"""Crew Members DM threads: binding persistence, routes, and pin enforcement.

Covers spec task 2 of the Crew Members page:

* ``dm.json`` binding read/write in the existing per-member space
  (``$KIROCREW_HOME/members/<slug>/``, isolated per test by the autouse
  ``_isolate_kirocrew_home`` fixture).
* Route contract for ``GET /api/members`` and the idempotent
  ``POST /api/members/{slug}/thread``.
* Agent-pin enforcement at every reachable writer: the send-path slot config,
  the agent-switch endpoint, and the mid-turn ``EVENT_AGENT_SWITCHED`` veto.
  Each denial test also asserts the slot state did NOT move (the mutation
  check: removing the guard makes these fail by letting the write land).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.dashboard.chat_handlers import _history_key_for
from kiro_crew.members import (
    DM_SLOT_MODE,
    MemberSlugError,
    dm_binding_path,
    member_dir,
    member_slot_key,
    read_dm_binding,
    write_dm_binding,
)

CREW = "code-reviewer"
OTHER = "other-agent"


def _fake_config(names, default=CREW):
    return SimpleNamespace(
        agents={name: KiroCrewAgentConfig(kiro_agent=name) for name in names},
        default_agent=default,
    )


class TestDmBinding:
    def test_write_then_read_round_trips(self):
        write_dm_binding("code-reviewer", member=CREW, slot_key="member-code-reviewer")
        binding = read_dm_binding("code-reviewer")
        assert binding is not None
        assert binding["member"] == CREW
        assert binding["slug"] == "code-reviewer"
        assert binding["slot_key"] == "member-code-reviewer"
        assert binding["created_ts"]

    def test_binding_lives_inside_the_trust_subtree(self):
        write_dm_binding("code-reviewer", member=CREW, slot_key="member-code-reviewer")
        path = dm_binding_path("code-reviewer")
        # Identity authority belongs under the keystone-gated trust/ subtree,
        # NOT inside the agent-writable member dir.
        assert "trust" in path.parts
        assert not (member_dir("code-reviewer") / "dm.json").exists()
        assert path.is_file()
        # Plain JSON on disk (atomic_write leaves no temp siblings behind).
        assert json.loads(path.read_text(encoding="utf-8"))["member"] == CREW
        # atomic_write's temps are `*.tmp` siblings; assert on that pattern so
        # a leaked temp is actually observable (a `*.json` glob never sees one).
        assert not list(path.parent.glob("*.tmp"))
        assert list(path.parent.glob("*.json")) == [path]

    def test_read_missing_file_is_none(self):
        assert read_dm_binding("nobody-here") is None

    def test_read_bad_slug_is_none(self):
        assert read_dm_binding("Not A Slug!") is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not json {",
            json.dumps(["a", "list"]),
            json.dumps({"member": CREW}),  # no slot_key
            json.dumps({"slot_key": ""}),  # empty slot_key
            json.dumps({"slot_key": "member-x"}),  # no member
            json.dumps({"slot_key": 7, "member": CREW}),  # non-string slot_key
        ],
    )
    def test_read_malformed_payload_is_none(self, payload):
        path = dm_binding_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        assert read_dm_binding("code-reviewer") is None

    def test_read_invalid_utf8_is_none(self):
        """Invalid UTF-8 bytes are the same totality case as unreadable IO.

        A raise here would 500 every member API off one corrupt dm.json."""
        path = dm_binding_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"member": "\xff\xfe broken')
        assert read_dm_binding("code-reviewer") is None

    def test_write_rejects_bad_slug(self):
        with pytest.raises(MemberSlugError):
            write_dm_binding("../escape", member=CREW, slot_key="member-x")

    def test_non_canonical_slot_key_reads_as_absent(self):
        """A binding whose slot_key is not the slug's derivation is unusable.

        dm.json pointing anywhere else would let a tampered or stale file mount
        an arbitrary session as the member's thread (the roster trusts `bound`
        rows enough that the page skips the create POST). Non-canonical reads
        as None, so the thread endpoint repairs it to the derived key.
        """
        path = dm_binding_path("code-reviewer")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"member": CREW, "slot_key": "dashboard:chat-7-123"}),
            encoding="utf-8",
        )
        assert read_dm_binding("code-reviewer") is None

    def test_slot_key_derivation(self):
        assert member_slot_key("code-reviewer") == "member-code-reviewer"

    def test_slot_key_rejects_bad_slug(self):
        with pytest.raises(MemberSlugError):
            member_slot_key("Bad Slug")


def _make_members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import (
        api_member_activity,
        api_member_thread,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    app.router.add_post("/api/members/{slug}/thread", api_member_thread)
    app.router.add_get("/api/members/{slug}/activity", api_member_activity)
    return app


def _patched_config(names, default=CREW):
    return patch(
        "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
        return_value=_fake_config(names, default),
    )


class TestMemberRoutes:
    @pytest.mark.asyncio
    async def test_roster_lists_global_crews_with_slugs(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW, "Docs_Writer"]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        rows = {r["name"]: r for r in data["members"]}
        assert rows[CREW]["slug"] == "code-reviewer"
        assert rows["Docs_Writer"]["slug"] == "docs-writer"
        assert rows[CREW]["slot_key"] == ""
        assert rows[CREW]["running"] is False
        # The row is an explicit allowlist: no dataclass spread, no `bound`
        # (the page never trusts it), no top-level default_agent.
        assert "bound" not in rows[CREW]
        assert "default_agent" not in data
        # The avatar override IS allowlisted (presentation-only, validated at
        # load) — without it every Members surface shows the name-derived face.
        assert rows[CREW]["avatar"] == {}
        # Unbound members have never talked: last activity reads as 0.
        assert rows[CREW]["last_active_ts"] == 0.0

    @pytest.mark.asyncio
    async def test_roster_reports_last_activity_from_the_dm_transcript(self, tmp_path):
        """last_active_ts is the DM transcript's mtime — the roster's sort key.

        The transcript file is the one durable signal that survives restarts
        and covers live and dormant threads alike; a bound member with no
        transcript still reads 0 rather than erroring.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        state.conversation_log.append(key, "user", "hello")
        with _patched_config([CREW, "Docs_Writer"]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        rows = {r["name"]: r for r in data["members"]}
        assert rows[CREW]["last_active_ts"] > 0
        # The row also carries the transcript's last-message preview — the
        # roster sub-line, same data a session row shows.
        assert rows[CREW]["last_message"] == "hello"
        # A member with no transcript stays at 0 — sorted last, never an error.
        assert rows["Docs_Writer"]["last_active_ts"] == 0.0
        assert rows["Docs_Writer"]["last_message"] == ""

    @pytest.mark.asyncio
    async def test_roster_preview_redacts_before_truncation(self, tmp_path):
        """A credential straddling the preview's length cap never leaks.

        Redaction runs on the FULL text before the 120-char cap: truncating
        first splits the token, and the pattern-based redactors cannot match
        a partial credential — its raw prefix would reach /api/members.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        # Padding places the AKIA token across the 120-char boundary.
        secret = "AKIAIOSFODNN7EXAMPLE"
        state.conversation_log.append(key, "assistant", "x" * 110 + " " + secret)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        preview = {r["name"]: r for r in data["members"]}[CREW]["last_message"]
        # Neither the full token nor any partial prefix of it survives.
        assert "AKIA" not in preview

    @pytest.mark.asyncio
    async def test_roster_orders_by_message_ts_not_file_mtime(self, tmp_path):
        """last_active_ts is the newest MESSAGE's own timestamp.

        Non-message writes (metadata, rehydration) bump the transcript file's
        mtime without any new message; ordering on mtime made rows reorder
        with no visible cause. Bumping the older thread's file mtime to the
        newest time must NOT promote it above the thread whose message is
        actually newer.
        """
        import os
        import time

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        write_dm_binding(
            "docs-writer", member="Docs_Writer", slot_key=member_slot_key("docs-writer")
        )
        old_key = f"dashboard:{member_slot_key(CREW)}"
        new_key = f"dashboard:{member_slot_key('docs-writer')}"
        state.conversation_log.append(old_key, "user", "older message")
        state.conversation_log.append(new_key, "user", "newer message")
        # Touch the OLDER thread's file so its mtime is the newest of the two.
        old_path = state.conversation_log._path(old_key)
        now = time.time() + 60
        os.utime(old_path, (now, now))
        with _patched_config([CREW, "Docs_Writer"]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                data = await resp.json()
        rows = {r["name"]: r for r in data["members"]}
        assert rows[CREW]["last_active_ts"] < rows["Docs_Writer"]["last_active_ts"]

    @pytest.mark.asyncio
    async def test_thread_create_then_roster_reports_bound(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/code-reviewer/thread")
                assert resp.status == 200
                created = await resp.json()
                resp = await client.get("/api/members")
                data = await resp.json()
        assert created == {
            "slot_key": "member-code-reviewer",
            "slug": "code-reviewer",
            "member": CREW,
        }
        row = data["members"][0]
        assert row["slot_key"] == "member-code-reviewer"
        slot = state._slots["member-code-reviewer"]
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE

    @pytest.mark.asyncio
    async def test_thread_reopen_rehydrates_dormant_history(self, tmp_path):
        """A dormant thread's transcript comes back when the thread reopens.

        Gateway restart outside the restore window (or a ✕-closed thread)
        leaves the canonical transcript on disk with no live slot. Minting a
        bare slot would reopen the DM with EMPTY context — the next reply
        would run without the prior conversation.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "remember the roadmap discussion")
        log.append(key, "assistant", "noted: roadmap discussion")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE})
        assert member_slot_key(CREW) not in state._slots
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 200
        slot = state._slots[member_slot_key(CREW)]
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE
        # The reopened slot carries the prior DM context, not an empty pane.
        assert any(
            "roadmap discussion" in str(m.get("content", "")) for m in slot.messages
        ), "dormant history was not rehydrated into the reopened thread"

    @pytest.mark.asyncio
    async def test_thread_create_is_idempotent(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                first = await (await client.post("/api/members/code-reviewer/thread")).json()
                second = await (await client.post("/api/members/code-reviewer/thread")).json()
        assert first["slot_key"] == second["slot_key"]
        # Idempotent: the second open returns the same body — repair vs create
        # is not a client-visible distinction.
        assert first == second
        assert len([k for k in state._slots if k.startswith("member-")]) == 1

    @pytest.mark.asyncio
    async def test_thread_invalid_slug_is_400_with_code(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/Not-A-Slug/thread")
                assert resp.status == 400
                assert (await resp.json())["code"] == "invalid_member_slug"
        assert not state._slots

    @pytest.mark.asyncio
    async def test_thread_unknown_member_is_404_with_code(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/nobody-here/thread")
                assert resp.status == 404
                assert (await resp.json())["code"] == "member_not_found"

    @pytest.mark.asyncio
    async def test_thread_refuses_a_foreign_slot_on_the_derived_key(self, tmp_path):
        """A pre-existing non-member slot occupying member-<slug> is never adopted.

        The constructor now refuses to MINT such a squatter, so this simulates
        a legacy one (pre-reservation install) by inserting the slot directly —
        the endpoint must still refuse to adopt it.
        """
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        foreign = _ChatSlot("member-code-reviewer", agent="someone-else")
        state._slots[foreign.key] = foreign
        assert foreign.mode != DM_SLOT_MODE
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post("/api/members/code-reviewer/thread")
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_slot_conflict"
        # Mutation check: the foreign slot was not converted or re-pinned.
        assert foreign.agent == "someone-else"
        assert read_dm_binding("code-reviewer") is None

    @pytest.mark.asyncio
    async def test_colliding_slug_stays_with_first_bound_member(self, tmp_path):
        """Two crew names deriving one slug: the binding's member wins."""
        state = _make_state(tmp_path)
        # Both fold to "review-agent"; config order makes Review_Agent first.
        with _patched_config(["Review_Agent", "review-agent"], default="Review_Agent"):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                first = await (await client.post("/api/members/review-agent/thread")).json()
                second = await (await client.post("/api/members/review-agent/thread")).json()
                roster = await (await client.get("/api/members")).json()
        assert first["member"] == "Review_Agent"
        assert second["member"] == "Review_Agent"
        bound_rows = [r for r in roster["members"] if r["slot_key"]]
        assert [r["name"] for r in bound_rows] == ["Review_Agent"]

    @pytest.mark.asyncio
    async def test_app_tokens_are_denied(self, tmp_path):
        state = _make_state(tmp_path)

        @web.middleware
        async def _as_app(request: web.Request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_members_app(state)
        app.middlewares.insert(0, _as_app)
        with _patched_config([CREW]):
            async with TestClient(TestServer(app)) as client:
                assert (await client.get("/api/members")).status == 404
                assert (await client.post("/api/members/code-reviewer/thread")).status == 404
                assert (await client.get("/api/members/code-reviewer/activity")).status == 404
        assert not state._slots

    @pytest.mark.asyncio
    async def test_member_slot_is_excluded_from_the_chat_surface(self, tmp_path):
        """mode="member" is what keeps the thread out of the Sessions list.

        The frontend's single ownership predicate (isChatPageSurface) admits
        only ''/'orchestrator'/'crew'; the serialized payload's mode/surface
        pair is the contract this pins.
        """
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                await client.post("/api/members/code-reviewer/thread")
        payload = state._slots["member-code-reviewer"].to_dict()
        assert payload["mode"] == DM_SLOT_MODE
        assert payload["surface"] == DM_SLOT_MODE


def _member_slot(state, key="member-code-reviewer", agent=CREW):
    # A real member slot is born via POST /api/members/{slug}/thread, which
    # always writes the binding first — mirror that here so the send path's
    # binding-drift guard sees the legitimate state.
    write_dm_binding(key[len("member-") :], member=agent, slot_key=key)
    slot = state.get_or_create_slot(key, agent=agent, mode=DM_SLOT_MODE)
    assert slot.mode == DM_SLOT_MODE
    return slot


class TestPinEnforcement:
    @pytest.mark.asyncio
    async def test_agent_switch_endpoint_refuses_member_repin(self, tmp_path):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": OTHER})
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_thread_agent_pinned"
        # Mutation check: the pin held — nothing rebound the slot.
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_send_path_refuses_member_agent_mismatch(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat", json={"slot": slot.key, "agent": OTHER, "message": "hi"}
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_thread_agent_pinned"
        assert slot.agent == CREW
        # The refused send dispatched nothing into the thread.
        assert not any(m.get("content") == "hi" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_send_path_allows_matching_or_absent_agent(self, tmp_path):
        """The pin refuses MISMATCHES only.

        An empty message stops the request at the message-required check, which
        sits AFTER the pin guard — reaching that 400 instead of the pin's 409
        proves the guard fell through for a matching/absent agent, without
        dispatching a real turn.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                for body in (
                    {"slot": slot.key, "agent": CREW, "message": ""},
                    {"slot": slot.key, "message": ""},
                ):
                    resp = await client.post("/api/chat", json=body)
                    assert resp.status == 400
                    assert (await resp.json()).get("code") != "member_thread_agent_pinned"
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_send_path_fails_closed_on_binding_drift(self, tmp_path):
        """A live member slot whose binding vanished must refuse the send.

        Binding deleted/corrupted while the tab stays open -> accepting the
        send would persist history that restore and thread-open both refuse,
        stranding the transcript the moment the slot dies. Empty message +
        409 (not the message-required 400) proves the guard ran first.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        # Binding vanishes out from under the live slot.
        dm_binding_path(CREW).unlink()
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json={"slot": slot.key, "message": ""})
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_binding_missing"
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_send_path_fails_closed_on_registry_drift(self, tmp_path):
        """An agentless send must not dispatch for a crew the registry lost.

        Crew deleted while its thread stays open -> the resolver would fall
        back to the default agent and store the reply under the deleted
        member's identity. The guard fires BEFORE the message-required check,
        so an empty message reaching 409 (not 400) proves it ran first.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        # Registry no longer contains CREW — only an unrelated crew.
        with _patched_config([OTHER]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json={"slot": slot.key, "message": ""})
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_pin_mismatch"
        assert slot.agent == CREW

    def _runner_harness(self, tmp_path, *, mode):
        from kiro_crew.dashboard.chat_runner import _run_chat

        state = _make_state(tmp_path)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.set_approval_policy = MagicMock()
        state.sessions.check_context_usage = MagicMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.is_yolo_active = MagicMock(return_value=False)
        state._background_tasks = set()

        # Ordinary-mode control runs on an ordinary key: the constructor's
        # member-* reservation (correctly) refuses a bare member key.
        slot_key = "member-code-reviewer" if mode == DM_SLOT_MODE else "chat-1-100"
        slot = state.get_or_create_slot(slot_key, agent=CREW, mode=mode)
        slot.append("user", "hello", "msg msg-u")

        client = state.sessions.get_or_create.return_value[0]
        client.shutdown = AsyncMock()

        from kiro_crew.providers.base import (
            EVENT_AGENT_SWITCHED,
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        async def _stream(msg):
            yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text=OTHER)
            # Anything after the switch executes as the FOREIGN agent — the
            # veto must stop consumption here, so this text must never land.
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="foreign agent output after switch")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client.stream = _stream
        client.stream_command = _stream
        return state, slot, _run_chat

    @pytest.mark.asyncio
    async def test_mid_turn_agent_switch_is_vetoed_on_member_threads(self, tmp_path):
        state, slot, _run_chat = self._runner_harness(tmp_path, mode=DM_SLOT_MODE)

        await _run_chat(state, slot, "test message")

        # The pin held: agent unchanged, no switch advertised to the UI.
        assert slot.agent == CREW
        switch_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "slot_agent_switch"
        ]
        assert switch_broadcasts == []
        # The veto is VISIBLE: kiro-cli already switched, so the rest of the
        # turn runs as the foreign agent — the thread must say so.
        assert any(
            "Agent switch" in str(m.get("content", "")) and "pinned" in str(m.get("content", ""))
            for m in slot.messages
        ), "veto left no user-visible notice on the thread"
        # The stream was TERMINATED at the veto: kiro-cli had already switched,
        # so any later event would execute as the foreign agent — the text the
        # harness yields after the switch must never land on the thread.
        assert not any(
            "foreign agent output" in str(m.get("content", "")) for m in slot.messages
        ), "events after the vetoed switch were still consumed"
        # And it is CONSUMED: the finally block resets the session so the next
        # turn cold-starts on the pinned crew. Dropping needs_session_reset in
        # the veto branch fails this line.
        state.sessions.reset.assert_awaited()
        # The veto counts as VISIBLE OUTPUT: without that, the empty-response
        # recovery would silently requeue the prompt and REPLAY any
        # non-idempotent tool calls that completed before the switch event.
        assert slot._empty_response_retries == 0, (
            "vetoed turn triggered the empty-response requeue — completed "
            "tool side effects would replay"
        )

    @pytest.mark.asyncio
    async def test_mid_turn_agent_switch_still_lands_on_ordinary_slots(self, tmp_path):
        """Control for the veto: the same event MOVES a non-member slot.

        Proves the event path executes in this harness, so the member test
        above passes because of the veto, not because the event never ran.
        """
        state, slot, _run_chat = self._runner_harness(tmp_path, mode="")

        await _run_chat(state, slot, "test message")

        assert slot.agent == OTHER
        switch_broadcasts = [
            c
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "slot_agent_switch"
        ]
        assert len(switch_broadcasts) == 1


def _mode_app(state) -> web.Application:
    from kiro_crew.dashboard.chat_folders import api_chat_slot_mode

    app = web.Application()
    app["state"] = state
    app.router.add_patch("/api/chat/slots/{slot}/mode", api_chat_slot_mode)
    return app


class TestModeLock:
    """The mode writer is the one door that would unlock every pin guard."""

    @pytest.mark.asyncio
    async def test_mode_patch_refuses_member_slots(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _member_slot(state)
        async with TestClient(TestServer(_mode_app(state))) as client:
            resp = await client.patch(f"/api/chat/slots/{slot.key}/mode", json={"mode": ""})
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_mode_locked"
        # Mutation check: the guard held — mode (the pin's predicate) unmoved.
        assert slot.mode == DM_SLOT_MODE

    @pytest.mark.asyncio
    async def test_mode_patch_still_serves_ordinary_slots(self, tmp_path):
        """Control: the lock is member-scoped, not a blanket refusal."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1-100", mode="")
        async with TestClient(TestServer(_mode_app(state))) as client:
            resp = await client.patch(f"/api/chat/slots/{slot.key}/mode", json={"mode": ""})
            assert resp.status != 409


class TestResumeGuards:
    """Resume is the transcript-restore path; it may not re-bind a pin."""

    @pytest.mark.asyncio
    async def test_resume_refuses_foreign_transcript_on_member_key(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        # A transcript persisted by an ORDINARY session of another agent.
        log = state.conversation_log
        log.append("dashboard:chat-9-1", "user", "hello")
        log.update_metadata("dashboard:chat-9-1", {"agent": OTHER, "mode": ""})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume",
                json={"key": "dashboard:chat-9-1"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_thread_agent_pinned"
        # Mutation check: the refusal came BEFORE slot creation, so no
        # non-member landmine occupies the member key (which would 409 the
        # real thread opener forever).
        assert member_slot_key(CREW) not in state._slots

    @pytest.mark.asyncio
    async def test_resume_refuses_member_transcript_on_ordinary_key(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:stolen", "user", "hello")
        log.update_metadata("dashboard:stolen", {"agent": CREW, "mode": DM_SLOT_MODE})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/ordinary-1/resume", json={"key": "dashboard:stolen"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_mode_key_mismatch"
        assert "ordinary-1" not in state._slots

    @pytest.mark.asyncio
    async def test_rejected_member_resume_leaves_closed_flag_intact(self, tmp_path):
        """A resume the member guard refuses must not mutate durable state.

        The refusal must run BEFORE ``clear_closed`` / ``_unhide_folder``: a
        member key with no binding is going to 409, and that doomed request
        silently reopening a closed member thread (or unhiding its folder)
        is exactly the side effect the early guard exists to prevent.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        # NO binding written: the member key resume will be refused.
        log = state.conversation_log
        key = _history_key_for(member_slot_key(CREW))
        log.append(key, "user", "hello")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE, "closed": time.time()})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_thread_agent_pinned"
        # The closed flag survived the rejected resume.
        assert log.get_metadata(key).get("closed"), "rejected resume cleared 'closed'"
        assert member_slot_key(CREW) not in state._slots

    @pytest.mark.asyncio
    async def test_resume_serves_the_member_thread_its_own_transcript(self, tmp_path):
        """Control: a member thread's own history restores onto its own key."""
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "hello")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 200
        slot = state._slots[member_slot_key(CREW)]
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE

    @pytest.mark.asyncio
    async def test_concurrent_member_resume_does_not_duplicate_history(self, tmp_path):
        """A resume racing the binding await must yield to the winner's slot.

        The late binding read is the one suspension point between the earlier
        live-slot re-checks and the publish. A concurrent resume that
        publishes during it must be SEEN: the loser answers with the live
        slot instead of get_or_create-ing the existing slot and hydrating the
        disk transcript onto it a second time (duplicated history on the next
        flush).
        """
        from unittest.mock import patch as _patch

        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "hello")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE})

        real_read = read_dm_binding

        def _publish_mid_await(slug):
            # Simulate the concurrent WINNER: it published the slot (and
            # hydrated the one disk message) while this request was suspended
            # in the binding read.
            slot = state.get_or_create_slot(member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE)
            slot.append("user", "hello", "msg msg-u")
            return real_read(slug)

        with _patch(
            "kiro_crew.members.read_dm_binding",
            side_effect=_publish_mid_await,
        ):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
                )
                assert resp.status == 200
        slot = state._slots[member_slot_key(CREW)]
        # The loser did NOT hydrate a second copy of the transcript.
        hellos = [m for m in slot.messages if m.get("content") == "hello"]
        assert len(hellos) == 1, f"history duplicated: {len(hellos)} copies"

    @pytest.mark.asyncio
    async def test_resume_of_a_closed_member_thread_succeeds(self, tmp_path):
        """A CLOSED member thread's legitimate resume must not self-409.

        The resume path clears the ``closed`` flag before the member identity
        barrier runs; the barrier's baseline must absorb that self-inflicted
        mutation (exactly ``closed``/``closed_at``) or every closed-thread
        resume trips it — a 409 issued AFTER the reopen durably landed,
        leaving the archive corrupted (reopened on disk, refused on the wire).
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "hello")
        log.update_metadata(
            key,
            {"agent": CREW, "mode": DM_SLOT_MODE, "closed": True, "closed_at": time.time() - 60},
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 200, await resp.json()
        # The reopen landed AND the resume was served — consistent state.
        assert not log.get_metadata(key).get("closed")
        slot = state._slots[member_slot_key(CREW)]
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE

    @pytest.mark.asyncio
    async def test_resume_denies_app_tokens_uniformly(self, tmp_path):
        """An app token gets the isolation 404 before the binding is read.

        Resuming a canonical member history would otherwise hydrate the
        member's transcript into an app-reachable slot; the uniform 404 also
        keeps the member-* space unenumerable (same answer whether or not
        the history exists).
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        state.conversation_log.append(key, "user", "private member content")

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        assert member_slot_key(CREW) not in state._slots

    @pytest.mark.asyncio
    async def test_resume_refuses_when_metadata_changes_across_the_binding_read(
        self, tmp_path, monkeypatch
    ):
        """Delete/recreate during the binding await must not corrupt the replacement.

        Messages are read BEFORE the binding await; metadata is re-read after
        it. If the transcript is replaced in between, the old messages would
        hydrate against the replacement metadata and the next flush would
        overwrite the replacement transcript. The identity barrier compares
        the two metadata snapshots and refuses on any drift.
        """
        from chat_test_helpers import _make_app

        import kiro_crew.members as members_real

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "old incarnation message")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE, "title": "old"})

        real_read = members_real.read_dm_binding

        def _racing_read(slug):
            # The replacement lands DURING the binding await — after the
            # message read, before the metadata re-read.
            log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE, "title": "replaced"})
            return real_read(slug)

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.members_mod.read_dm_binding", _racing_read
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_resume_conflict"
        # Nothing was hydrated onto the key — reopening reads fresh.
        assert member_slot_key(CREW) not in state._slots


class TestRegistryMovedUnderBinding:
    @pytest.mark.asyncio
    async def test_thread_fails_closed_when_the_bound_slot_runs_a_dead_crew(self, tmp_path):
        """Crew renamed/deleted, same slug: the endpoint refuses, re-entrantly.

        Re-pinning here would be an agent switch that skips every invariant the
        real switch endpoint holds (slot lock, workspace/project re-resolution,
        pending-wait unblocking, metadata persistence, broadcast). So the
        endpoint fails closed with its own code, mutates nothing, and leaves
        the binding untouched so the refusal repeats until a human resolves it
        in the crew manager.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(member_slot_key(CREW), agent="Dead_Crew", mode=DM_SLOT_MODE)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 409
                body = await resp.json()
                assert body["code"] == "member_pin_mismatch"
        # Mutation checks: nothing moved, and no binding was written — the
        # refusal is re-entrant instead of self-erasing.
        assert slot.agent == "Dead_Crew"
        assert read_dm_binding(CREW) is None


class TestOpenAiCompatPin:
    @pytest.mark.asyncio
    async def test_completions_refuses_member_agent_mismatch(self):
        """The OpenAI-compat per-request agent write honors the pin."""
        import asyncio as _asyncio

        from kiro_crew.dashboard.openai_compat import api_completions
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        class _Ready(KiroPrerequisiteService):
            async def session_ready(self) -> bool:  # pragma: no cover - trivial
                return True

            async def verified_ready(self, *, max_age_secs: float) -> bool:
                del max_age_secs
                return True

        slot = MagicMock()
        slot.key = member_slot_key(CREW)
        slot.agent = CREW
        slot.mode = DM_SLOT_MODE
        slot.task = None
        slot.event = _asyncio.Event()
        slot.drain = MagicMock(return_value=[])
        state = MagicMock()
        state.get_or_create_slot = MagicMock(return_value=slot)
        state._slots = {slot.key: slot}
        state._background_tasks = set()

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "model": "auto",
                "agent": OTHER,
                "slot": slot.key,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        request.app = {
            "state": state,
            "kiro_prerequisite_service": object.__new__(_Ready),
        }
        request.get = MagicMock(side_effect=lambda k, d="": d)

        resp = await api_completions(request)
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["error"]["code"] == "member_thread_agent_pinned"
        # Mutation check: the pin held.
        assert slot.agent == CREW

    @pytest.mark.asyncio
    async def test_completions_fail_closed_on_binding_drift(self):
        """The OpenAI-compat send also refuses when the binding vanished.

        Mirrors the chat_send binding-drift guard: a deleted/corrupt dm.json
        must not let a completion dispatch on the live member slot and
        persist a transcript restore skips and thread-open refuses. No
        binding is written here (the pinned test HOME starts empty), so the
        guard sees exactly the drifted state.
        """
        import asyncio as _asyncio

        from kiro_crew.dashboard.openai_compat import api_completions
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        class _Ready(KiroPrerequisiteService):
            async def session_ready(self) -> bool:  # pragma: no cover - trivial
                return True

            async def verified_ready(self, *, max_age_secs: float) -> bool:
                del max_age_secs
                return True

        slot = MagicMock()
        slot.key = member_slot_key(CREW)
        slot.agent = CREW
        slot.mode = DM_SLOT_MODE
        slot.task = None
        slot.event = _asyncio.Event()
        slot.drain = MagicMock(return_value=[])
        state = MagicMock()
        state.get_or_create_slot = MagicMock(return_value=slot)
        state._slots = {slot.key: slot}
        state._background_tasks = set()

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "model": CREW,  # model maps to agent: matching passes the pin, reaches drift checks
                "id": slot.key,  # "id" (not "slot") is how existing slots are addressed here
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        request.app = {
            "state": state,
            "kiro_prerequisite_service": object.__new__(_Ready),
        }
        request.get = MagicMock(side_effect=lambda k, d="": d)

        with _patched_config([CREW]):
            resp = await api_completions(request)
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["error"]["code"] == "member_binding_missing"
        assert body["code"] == "member_binding_missing"


class TestRegistryDriftWithoutLiveSlot:
    @pytest.mark.asyncio
    async def test_binding_naming_another_slugs_crew_reads_as_absent(self, tmp_path):
        """A tampered dm.json cannot point this slug's thread at another crew.

        dm.json in slug A's directory naming crew B (registered, different
        slug) would pin A's thread — and A's restored transcript — to B's
        identity. The read layer refuses it structurally: a member whose own
        slug differs from the directory's reads as no binding at all.
        """
        _ = tmp_path  # the autouse home fixture owns the data dir
        write_dm_binding(CREW, member="Totally_Different", slot_key=member_slot_key(CREW))
        assert read_dm_binding(CREW) is None
        # Same-slug names (collisions, renames) still read back.
        write_dm_binding(CREW, member="Code.Reviewer", slot_key=member_slot_key(CREW))
        assert read_dm_binding(CREW)["member"] == "Code.Reviewer"

    @pytest.mark.asyncio
    async def test_drifted_binding_fails_closed_even_with_no_live_slot(self, tmp_path):
        """The refusal keys off the BINDING, not a live slot.

        After a restart no live slot exists for most member threads, so a
        mismatch check against slot.agent alone would silently hand a renamed
        crew's same-slug successor the previous crew's entire transcript —
        same key, same history, successor's name on the pin chip. The binding
        naming a non-owner must refuse BEFORE any slot is created or dm.json
        is rewritten.
        """
        state = _make_state(tmp_path)
        write_dm_binding(CREW, member="Code.Reviewer", slot_key=member_slot_key(CREW))
        # Registry knows only the same-slug successor; no slot is live.
        with patch(
            "kiro_crew.dashboard.handlers.members._member_names_for_slug",
            return_value=[CREW],
        ):
            with _patched_config([CREW]):
                async with TestClient(TestServer(_make_members_app(state))) as client:
                    resp = await client.post(f"/api/members/{CREW}/thread")
                    assert resp.status == 409
                    body = await resp.json()
                    assert body["code"] == "member_pin_mismatch"
        # Mutation checks: no slot created, binding untouched (re-entrant).
        assert member_slot_key(CREW) not in state._slots
        assert read_dm_binding(CREW)["member"] == "Code.Reviewer"


class TestReservedMemberKeys:
    @pytest.mark.asyncio
    async def test_mixed_case_member_resume_is_refused_not_500(self, tmp_path):
        """A mixed-case member key resume must 409, never crash.

        The constructor's reservation is casefolded, so `Member-radar` would
        raise ValueError there — the resume path does not catch it (HTTP
        500). The resume guards must therefore fold too: the mixed-case key
        hits the early pin guard, whose uppercase slug reads as unbound, and
        the request dies as a clean 409 before the constructor is reached.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        key = "dashboard:Member-radar"
        log = state.conversation_log
        log.append(key, "user", "hello")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/Member-radar/resume", json={"key": key})
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "member_thread_agent_pinned"
        assert "Member-radar" not in state._slots

    def test_reservation_is_case_insensitive(self, tmp_path):
        """Mixed-case member keys are reserved too.

        Transcript filenames derive from the slot key; on a case-insensitive
        filesystem (Windows, default macOS) "Member-radar" aliases
        "member-radar" — a mixed-case squatter passing a case-sensitive
        prefix check would corrupt or read the pinned thread's history
        through the alias.
        """
        state = _make_state(tmp_path)
        for squatter in ("Member-radar", "MEMBER-RADAR", "mEmBeR-radar"):
            with pytest.raises(ValueError):
                state.get_or_create_slot(squatter)
        # The canonical lowercase key with mode="member" still works.
        slot = state.get_or_create_slot("member-radar", agent=CREW, mode=DM_SLOT_MODE)
        assert slot.mode == DM_SLOT_MODE

    @pytest.mark.asyncio
    async def test_chat_send_cannot_auto_create_a_member_key(self, tmp_path):
        """member-* keys are born only through the member-thread endpoint.

        A send naming an ABSENT member key (e.g. racing a restart that dropped
        the live slot) must not mint an ordinary unpinned slot there — every
        pin guard keys on mode=="member", so a squatter bypasses all of them
        and 409s the real thread opener forever.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": member_slot_key(CREW), "message": "hi"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_slot_reserved"
        assert member_slot_key(CREW) not in state._slots

    @pytest.mark.asyncio
    async def test_chat_send_still_reaches_an_existing_member_slot(self, tmp_path):
        """Control: the reservation blocks CREATION, not conversation.

        An empty message on the live thread reaches the message-required 400
        (past both the reservation and the pin), proving the guard admits the
        legitimate path.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot = _member_slot(state)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json={"slot": slot.key, "message": ""})
                assert resp.status == 400
                assert (await resp.json()).get("code") != "member_slot_reserved"


class TestPersistenceRestoreGate:
    def test_member_identity_resolves_from_the_binding(self):
        from kiro_crew.dashboard.chat_persistence import _member_restore_identity

        key = member_slot_key(CREW)
        write_dm_binding(CREW, member=CREW, slot_key=key)
        assert _member_restore_identity(key) == (CREW, DM_SLOT_MODE)
        # Ordinary keys are not member restores at all.
        assert _member_restore_identity("chat-1-1") is None

    def test_member_key_without_binding_is_skipped_not_published(self):
        """No binding -> the restore skips the slot instead of publishing it.

        Publishing would need a bare member key through the constructor's
        reservation (refused), and downgrading would squat the key. Skipping
        loses nothing: the transcript stays on disk and the member-thread
        endpoint re-creates and re-binds the slot on the next page open.
        """
        from kiro_crew.dashboard.chat_persistence import (
            _SKIP_MEMBER_RESTORE,
            _member_restore_identity,
        )

        assert _member_restore_identity(member_slot_key(CREW)) is _SKIP_MEMBER_RESTORE

    def test_open_slot_restore_round_trips_a_bound_member_thread(self, tmp_path):
        """End to end: a bound member thread survives a restart pinned.

        This is the invariant round 3's constructor reservation accidentally
        broke (a bare member key was refused before the binding was consulted);
        the binding-first resolution is what re-admits the legitimate restore.
        """
        from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        key = member_slot_key(CREW)
        write_dm_binding(CREW, member=CREW, slot_key=key)
        log = state.conversation_log
        log.append(f"dashboard:{key}", "user", "hello")
        # Transcript metadata is deliberately WRONG about the pin: the binding
        # must win over both fields.
        log.update_metadata(f"dashboard:{key}", {"agent": OTHER, "mode": ""})
        slot = _rehydrate_slot_from_history(state, key)
        assert slot is not None
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE

    def test_open_slot_restore_skips_a_member_key_without_binding(self, tmp_path):
        from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

        state = _make_state(tmp_path)
        key = member_slot_key(CREW)
        log = state.conversation_log
        log.append(f"dashboard:{key}", "user", "hello")
        log.update_metadata(f"dashboard:{key}", {"agent": CREW, "mode": DM_SLOT_MODE})
        assert _rehydrate_slot_from_history(state, key) is None
        assert key not in state._slots


class TestCentralReservation:
    """The reservation lives in the CONSTRUCTOR — one gate, every surface."""

    def test_get_or_create_slot_refuses_a_bare_member_key(self, tmp_path):
        state = _make_state(tmp_path)
        with pytest.raises(ValueError, match="member thread"):
            state.get_or_create_slot(member_slot_key(CREW))
        assert member_slot_key(CREW) not in state._slots

    def test_get_or_create_slot_admits_the_member_endpoint_shape(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE)
        assert slot.mode == DM_SLOT_MODE
        # And an existing member slot is returned as-is (idempotent open).
        assert state.get_or_create_slot(member_slot_key(CREW), mode=DM_SLOT_MODE) is slot

    @pytest.mark.asyncio
    async def test_slot_create_endpoint_cannot_mint_a_member_key(self, tmp_path):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots", json={"name": member_slot_key(CREW)})
            assert resp.status == 409
        assert member_slot_key(CREW) not in state._slots

    @pytest.mark.asyncio
    async def test_resume_creates_the_member_slot_pinned_from_the_binding(self, tmp_path):
        """The resumed slot's pin comes from dm.json, not from the transcript.

        Metadata whose agent was edited (or whose mode was lost) must not be
        able to re-pin or un-pin the thread: the slot is created with the
        binding's member and member mode BEFORE metadata restore runs, and the
        metadata arm is skipped for member keys.
        """
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "hello")
        # Tampered/degraded metadata: agent points elsewhere, mode is absent.
        log.update_metadata(key, {"agent": OTHER})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 200
        slot = state._slots[member_slot_key(CREW)]
        # The binding won on both fields.
        assert slot.agent == CREW
        assert slot.mode == DM_SLOT_MODE


class TestOrphanedHistory:
    @pytest.mark.asyncio
    async def test_missing_binding_with_existing_history_fails_closed(self, tmp_path):
        """A lost binding must not hand the transcript to whoever derives the slug.

        ChatPane hydrates from disk history BY KEY, so rebinding a slug whose
        canonical history already holds a conversation would render the
        previous occupant's transcript under the new crew's identity. With the
        binding gone, attribution is not re-derivable — refuse with its own
        code and leave everything untouched (re-entrant).
        """
        state = _make_state(tmp_path)
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "predecessor conversation")
        log.update_metadata(key, {"agent": "Old_Crew", "mode": DM_SLOT_MODE})
        # No dm.json on disk — the binding is gone, only the history remains.
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 409
                assert (await resp.json())["code"] == "member_binding_missing"
        assert member_slot_key(CREW) not in state._slots
        assert read_dm_binding(CREW) is None

    @pytest.mark.asyncio
    async def test_missing_binding_with_no_history_binds_fresh(self, tmp_path):
        """Control: a member key with no history is an ordinary first open."""
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.post(f"/api/members/{CREW}/thread")
                assert resp.status == 200
                assert (await resp.json())["member"] == CREW
        assert read_dm_binding(CREW)["member"] == CREW


class TestMemberActivityRoute:
    """GET /api/members/{slug}/activity — the drawer's timeline feed."""

    @pytest.mark.asyncio
    async def test_returns_recorded_entries_newest_first_with_allowlist_fields(self, tmp_path):
        state = _make_state(tmp_path)
        from kiro_crew.members import record_activity

        assert record_activity(CREW, "dashboard_chat-1", "persistent", via="chat")
        assert record_activity(
            CREW, "dashboard_chat-2", "persistent", project="/repo", via="select_crew"
        )
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["slug"] == "code-reviewer"
        assert data["member"] == CREW
        assert data["capped"] is False
        assert len(data["entries"]) == 2
        # Newest first — the drawer renders top-down.
        assert data["entries"][0]["via"] == "select_crew"
        assert data["entries"][0]["project"] == "/repo"
        assert data["entries"][0]["ts"] >= data["entries"][1]["ts"] > 0
        # Session keys stay OUT of the payload: the drawer renders what
        # happened, never handles into other sessions. This is the response's
        # field allowlist, pinned exactly.
        assert set(data["entries"][0]) == {"ts", "via", "project"}

    @pytest.mark.asyncio
    async def test_colliding_slugs_do_not_mix_histories(self, tmp_path):
        """Two names sharing a slug share a log file, never a timeline.

        Slugification is lossy ('Code Review' and 'code-review' both derive
        code-review), so the endpoint filters by the exact member name each
        record carries — one member's drawer must not render (or count) the
        other's events.
        """
        state = _make_state(tmp_path)
        from kiro_crew.members import record_activity

        other = "Code_Reviewer"  # distinct exact name, same derived slug
        assert record_activity(CREW, "dashboard_chat-1", "persistent", via="chat")
        assert record_activity(other, "dashboard_chat-2", "persistent", via="chat")
        with _patched_config([CREW, other]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                mine = await (
                    await client.get("/api/members/code-reviewer/activity", params={"member": CREW})
                ).json()
                theirs = await (
                    await client.get(
                        "/api/members/code-reviewer/activity", params={"member": other}
                    )
                ).json()
        assert len(mine["entries"]) == 1
        assert len(theirs["entries"]) == 1

    @pytest.mark.asyncio
    async def test_member_param_is_required(self, tmp_path):
        """Without the exact name a colliding slug's read is unsound, so the
        parameter is required by construction rather than caller discipline."""
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get("/api/members/code-reviewer/activity")
                assert resp.status == 400
                assert (await resp.json())["code"] == "missing_member"
                bad = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": "no spaces!"}
                )
                assert bad.status == 400

    @pytest.mark.asyncio
    async def test_empty_log_and_invalid_slug(self, tmp_path):
        state = _make_state(tmp_path)
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                assert (await resp.json())["entries"] == []
                # Path traversal / bad grammar refused before any file IO.
                bad = await client.get("/api/members/Bad_Slug!/activity", params={"member": CREW})
                assert bad.status == 400
                assert (await bad.json())["code"] == "invalid_member_slug"

    @pytest.mark.asyncio
    async def test_unreadable_timestamps_are_skipped_not_sorted_as_garbage(self, tmp_path):
        """A record without a parseable STRING ts cannot be placed on a
        timeline — including a numeric epoch from a foreign writer, which
        must read as unplaceable rather than crash the endpoint."""
        state = _make_state(tmp_path)
        from kiro_crew.members import ACTIVITY_FILE_NAME, member_dir, record_activity

        assert record_activity(CREW, "dashboard_chat-1", "persistent", via="chat")
        path = member_dir("code-reviewer") / ACTIVITY_FILE_NAME
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f'\n{{"ts": "not-a-date", "member": "{CREW}", "via": "chat"}}\n')
            fh.write(f'\n{{"ts": 1735689600, "member": "{CREW}", "via": "chat"}}\n')
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert len(data["entries"]) == 1

    @pytest.mark.asyncio
    async def test_project_values_are_redacted_at_the_boundary(self, tmp_path):
        """A project value is operator-supplied text that can embed a
        credential; the response is a network boundary, so it runs the same
        redaction chain as the roster's message preview."""
        state = _make_state(tmp_path)
        from kiro_crew.members import record_activity

        assert record_activity(
            CREW,
            "dashboard_chat-1",
            "persistent",
            project="/repos/AKIAIOSFODNN7EXAMPLE/app",
            via="chat",
        )
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert len(data["entries"]) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in data["entries"][0]["project"]

    @pytest.mark.asyncio
    async def test_display_cap_reports_capped_and_keeps_newest(self, tmp_path):
        """Entries beyond the display cap trim the OLDEST tail, and the
        response says the window is saturated so the drawer renders its
        derived counters as floors ("N+") instead of asserting exact totals."""
        state = _make_state(tmp_path)
        from kiro_crew.dashboard.handlers import members as handler_mod
        from kiro_crew.members import record_activity

        for i in range(handler_mod._ACTIVITY_LIMIT + 3):
            assert record_activity(CREW, f"dashboard_chat-{i}", "persistent", via="chat")
        with _patched_config([CREW]):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                resp = await client.get(
                    "/api/members/code-reviewer/activity", params={"member": CREW}
                )
                assert resp.status == 200
                data = await resp.json()
        assert data["capped"] is True
        assert len(data["entries"]) == handler_mod._ACTIVITY_LIMIT
