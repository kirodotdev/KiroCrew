"""Crew Members DM threads: the pin and the member-key reservation past the thread route.

The mode writer refuses member slots; resume restores only a member's own transcript onto
its own key; a ``member-*`` key is minted only by the member-thread endpoint and reserved
case-insensitively by the slot constructor; restart restore and resume take the pin from
``dm.json``, never the transcript; the OpenAI-compatible completions path honors the same
pin; and a configured free-form member stays a valid agent choice on an ordinary slot. A
binding that drifted from the registry, or was lost over existing history, fails closed
with its own code. Shared fakes live in ``members_dm_thread_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from members_dm_thread_helpers import (
    CREW,
    OTHER,
    _fake_config,
    _make_members_app,
    _member_slot,
    _patched_config,
)

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.chat_handlers import _history_key_for
from kiro_crew.members import (
    DM_SLOT_MODE,
    dm_binding_path,
    member_slot_key,
    read_dm_binding,
    slug_for_name,
    write_dm_binding,
)
from kiro_crew.testing.wait import LoopHop


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
    @pytest.mark.parametrize(
        ("window", "expected"),
        [
            # The agent read: the last await before the post-transcript re-check.
            ("restored_agent", ["check", "binding", "agent", "publish", "dedup"]),
            # The late binding read: the one await between that re-check and the publish.
            (
                "late_binding_read",
                ["check", "binding", "agent", "check", "binding", "publish", "dedup"],
            ),
        ],
    )
    async def test_concurrent_member_resume_does_not_duplicate_history(
        self, tmp_path, monkeypatch, window, expected
    ):
        """A resume racing an await before the publish must yield to the winner's slot.

        The handler re-checks for a live slot after each await that precedes the
        publish. A concurrent resume that publishes inside one of those windows must be
        SEEN by the re-check right after it: the loser answers with the live slot
        instead of get_or_create-ing the existing slot and hydrating the disk transcript
        onto it a second time (duplicated history on the next flush). Each case pins the
        ORDER of the awaits and re-checks, so the re-check that saw the winner is the
        one right after the window: deleting either re-check fails its case.

        The winner runs on the loop, where a real resume publishes, from the function
        the handler awaits in that window. It is never a side effect on
        ``members.read_dm_binding``: the member event log's legacy fold calls that on
        its ``eventlog-io`` worker, queued by the winner's own row, so a side effect
        there can fire a second time from that thread and append a second copy.
        """
        from chat_test_helpers import _make_app

        import kiro_crew.dashboard.chat_handlers as handlers

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "hello")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE})

        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        hop = LoopHop(loop)
        # The handler's awaits and re-checks, in order, with the winner's publish in the
        # window it landed in. A re-check that answers with the winner's slot is a
        # "dedup"; one that finds nothing is a "check".
        timeline: list[str] = []

        async def _publish_winner():
            slot = state.get_or_create_slot(member_slot_key(CREW), agent=CREW, mode=DM_SLOT_MODE)
            slot.append("user", "hello", "msg msg-u")

        def _publish_from_worker(where):
            # Both seams run under asyncio.to_thread. From the loop thread, waiting on
            # the loop would deadlock instead of failing.
            assert threading.get_ident() != loop_thread, f"{where} ran on the loop thread"
            hop.run(_publish_winner(), "the winner's publish")
            timeline.append("publish")

        real_agent = handlers._restored_agent_name
        real_read = handlers.members_mod.read_dm_binding_for_slot
        real_check = handlers._live_slot_for_resume

        def _agent(*args):
            timeline.append("agent")
            if window == "restored_agent":
                _publish_from_worker(window)
            return real_agent(*args)

        def _read_binding(slot_key):
            timeline.append("binding")
            # The late read is the binding read that follows the agent read.
            if window == "late_binding_read" and "agent" in timeline and "publish" not in timeline:
                _publish_from_worker(window)
            return real_read(slot_key)

        async def _check(*args, **kwargs):
            outcome = await real_check(*args, **kwargs)
            timeline.append("dedup" if outcome is not None and outcome.already_live else "check")
            return outcome

        monkeypatch.setattr(handlers, "_restored_agent_name", _agent)
        monkeypatch.setattr(handlers.members_mod, "read_dm_binding_for_slot", _read_binding)
        monkeypatch.setattr(handlers, "_live_slot_for_resume", _check)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            hop.check()
            assert resp.status == 200
        assert timeline == expected, (
            f"the winner's publish inside {window}, or the re-check that must see it, is "
            f"not where it belongs: {timeline}"
        )
        slot = state._slots[member_slot_key(CREW)]
        # The loser did NOT hydrate a second copy of the transcript.
        hellos = [m for m in slot.messages if m.get("content") == "hello"]
        assert len(hellos) == 1, f"history duplicated: {len(hellos)} copies: {hellos!r}"

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

        The replacement lands only inside the LATE binding read, so a re-read
        moved ahead of that await reads the old metadata and the refusal is
        lost.
        """
        from chat_test_helpers import _make_app

        import kiro_crew.dashboard.chat_handlers as handlers

        state = _make_state(tmp_path)
        write_dm_binding(CREW, member=CREW, slot_key=member_slot_key(CREW))
        key = f"dashboard:{member_slot_key(CREW)}"
        log = state.conversation_log
        log.append(key, "user", "old incarnation message")
        log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE, "title": "old"})

        real_agent = handlers._restored_agent_name
        real_read = handlers.members_mod.read_dm_binding_for_slot
        agent_read: list[bool] = []

        def _agent(*args):
            agent_read.append(True)
            return real_agent(*args)

        def _racing_read(slot_key):
            # The late read is the binding read that follows the agent read: after
            # the message read, before the metadata re-read.
            if agent_read:
                log.update_metadata(key, {"agent": CREW, "mode": DM_SLOT_MODE, "title": "replaced"})
            return real_read(slot_key)

        monkeypatch.setattr(handlers, "_restored_agent_name", _agent)
        monkeypatch.setattr(handlers.members_mod, "read_dm_binding_for_slot", _racing_read)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{member_slot_key(CREW)}/resume", json={"key": key}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_resume_conflict"
        assert (
            log.get_metadata(key).get("title") == "replaced"
        ), "the late binding read never ran, so nothing raced it"
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


class TestFreeFormMemberOnOrdinarySlot:
    """A configured free-form member is a valid agent CHOICE, not only a pin.

    The catalog lists ``dr. eggbot`` and the agent cycle sends its bare name to
    an ordinary slot; the choice guards must admit it. Anything off-grammar
    that is NOT a configured, dispatchable member stays refused.
    """

    NAME = "dr. eggbot"

    @pytest.mark.asyncio
    async def test_agent_switch_admits_a_configured_free_form_member(self, tmp_path):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("ordinary", agent="kirocrew")
        cfg = KiroCrewConfig()
        cfg.agents = {self.NAME: KiroCrewAgentConfig(kiro_agent="kirocrew")}
        cfg.default_agent = self.NAME
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
                resp = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": self.NAME}
                )
                assert resp.status == 200, await resp.text()
        assert slot.agent == self.NAME

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("configured", "requested"),
        [
            # Off-grammar and not a member: never a template, refused.
            (["dr. eggbot"], "dr. otherbot"),
            # A configured off-grammar member whose stored name requires redaction.
            (["crew password=shortvalue"], "crew password=shortvalue"),
        ],
    )
    async def test_agent_switch_still_refuses_off_grammar_non_members(
        self, tmp_path, configured, requested
    ):
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("ordinary", agent="kirocrew")
        cfg = _fake_config(configured, default=configured[0])
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
                resp = await client.post(
                    f"/api/chat/slots/{slot.key}/agent", json={"agent": requested}
                )
                assert resp.status == 400
                assert (await resp.json()) == {"error": "invalid agent name"}
        assert slot.agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_agent_switch_admits_a_published_dotted_template(self, tmp_path):
        """The template grammar (``TEMPLATE_NAME_RE``) admits ``reviewer.v2``; the
        choice guards must not hold a template to the stricter member-slot grammar.
        Reaching a non-400 answer proves the name guard fell through."""
        from chat_test_helpers import _make_app_with_agent_routes

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("ordinary", agent="kirocrew")
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer.v2"}
            )
            assert resp.status != 400 or (await resp.json()) != {"error": "invalid agent name"}
            bad = await client.post(
                f"/api/chat/slots/{slot.key}/agent", json={"agent": "reviewer.v2."}
            )
            assert bad.status == 400
            assert await bad.json() == {"error": "invalid agent name"}

    @pytest.mark.asyncio
    async def test_send_path_admits_a_configured_free_form_member(self, tmp_path):
        """Reaching the message-required 400 proves the name guard fell through."""
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        # An ORDINARY slot already running the member: the post-switch state.
        slot = state.get_or_create_slot("ordinary", agent=self.NAME)
        assert slot.mode != DM_SLOT_MODE
        cfg = _fake_config([self.NAME], default=self.NAME)
        with patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat", json={"slot": slot.key, "agent": self.NAME, "message": ""}
                )
                assert resp.status == 400, await resp.text()
                assert await resp.json() == {
                    "error": "message is required",
                    "code": "message_required",
                }
                bad = await client.post(
                    "/api/chat", json={"slot": slot.key, "agent": "dr. otherbot", "message": ""}
                )
                assert bad.status == 400
                assert await bad.json() == {"error": "invalid agent name"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("configured", "model", "admitted"),
        [(["dr. eggbot"], "dr. eggbot", True), (["dr. eggbot"], "dr. otherbot", False)],
    )
    async def test_completions_admit_only_a_configured_free_form_member(
        self, configured, model, admitted
    ):
        from kiro_crew.dashboard.openai_compat import api_completions
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        class _Ready(KiroPrerequisiteService):
            async def session_ready(self) -> bool:  # pragma: no cover - trivial
                return True

            async def verified_ready(self, *, max_age_secs: float) -> bool:
                del max_age_secs
                return True

        state = MagicMock()
        state._slots = {}
        request = MagicMock()
        request.json = AsyncMock(
            return_value={"model": model, "messages": [{"role": "user", "content": "hi"}]}
        )
        request.app = {"state": state, "kiro_prerequisite_service": object.__new__(_Ready)}
        request.get = MagicMock(side_effect=lambda key, default="": default)

        class _PastTheGuard(Exception):
            """Raised by ``_make_id``, the first statement after the name guard."""

        cfg = _fake_config(configured, default=configured[0])
        with (
            patch("kiro_crew.config.loader.KiroCrewConfig.load", return_value=cfg),
            patch("kiro_crew.dashboard.openai_compat._make_id", side_effect=_PastTheGuard),
        ):
            if admitted:
                with pytest.raises(_PastTheGuard):
                    await api_completions(request)
            else:
                response = await api_completions(request)
                assert response.status == 400
                body = json.loads(response.body)
                assert body["error"]["message"] == "invalid model/agent name"
        state.get_or_create_slot.assert_not_called()


class TestOpenAiCompatPin:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [False, True])
    @pytest.mark.parametrize(
        ("slot_id", "model"),
        [
            ("member-dr-eggbot", "dr. eggbot"),
            (member_slot_key("dr-eggbot", "private-store"), "kirocrew"),
        ],
    )
    async def test_app_completion_hides_member_slot_existence(self, live, slot_id, model):
        from kiro_crew.dashboard.openai_compat import api_completions
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        class _Ready(KiroPrerequisiteService):
            async def session_ready(self) -> bool:
                return True

            async def verified_ready(self, *, max_age_secs: float) -> bool:
                del max_age_secs
                return True

        state = MagicMock()
        state._slots = {}
        if live:
            slot = MagicMock()
            slot.key = slot_id
            slot.agent = model
            slot.mode = DM_SLOT_MODE
            state._slots[slot_id] = slot

        request = MagicMock()
        request.json = AsyncMock(
            return_value={
                "model": model,
                "id": slot_id,
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        request.app = {
            "state": state,
            "kiro_prerequisite_service": object.__new__(_Ready),
        }
        request.get = MagicMock(
            side_effect=lambda key, default="": "some-app" if key == "app" else default
        )

        response = await api_completions(request)
        assert response.status == 404
        assert json.loads(response.body) == {
            "error": {"message": "not found", "type": "invalid_request_error"},
            "code": "not_found",
        }

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
    @pytest.mark.parametrize("name", ["AKIAIOSFODNN7EXAMPLE", "crew password=shortvalue"])
    async def test_completion_refuses_a_redaction_requiring_stored_member_pin(self, name):
        import asyncio as _asyncio

        from kiro_crew.dashboard.openai_compat import api_completions
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        class _Ready(KiroPrerequisiteService):
            async def session_ready(self) -> bool:
                return True

            async def verified_ready(self, *, max_age_secs: float) -> bool:
                del max_age_secs
                return True

        slot = MagicMock()
        slot.key = "member-legacy-credential"
        slot.agent = name
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
                "model": name,
                "id": slot.key,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        request.app = {
            "state": state,
            "kiro_prerequisite_service": object.__new__(_Ready),
        }
        request.get = MagicMock(side_effect=lambda key, default="": default)

        cfg = _fake_config([name], default=name)
        with patch("kiro_crew.dashboard.openai_compat.KiroCrewConfig.load", return_value=cfg):
            response = await api_completions(request)
        assert response.status == 409
        body = json.loads(response.body)
        assert body["error"]["code"] == "member_pin_mismatch"
        assert body["code"] == "member_pin_mismatch"

    @pytest.mark.asyncio
    async def test_free_form_completion_reaches_binding_drift_guard(self):
        """A deleted or corrupt dm.json must refuse before dispatch.

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

        name = "dr. eggbot"
        slot = MagicMock()
        slot.key = member_slot_key("dr-eggbot", "member-dr-eggbot-generation")
        slot.agent = name
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
                "model": name,
                "id": slot.key,
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        request.app = {
            "state": state,
            "kiro_prerequisite_service": object.__new__(_Ready),
        }
        request.get = MagicMock(side_effect=lambda k, d="": d)

        cfg = _fake_config([name], default=name)
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
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
    async def test_invalid_bound_name_returns_coded_conflict(self, tmp_path):
        state = _make_state(tmp_path)
        invalid_name = "unsafe\nmember"
        slug = "unsafe-member"
        binding = write_dm_binding(slug, member=invalid_name, slot_key=member_slot_key(slug))
        binding["member_id"] = ""
        dm_binding_path(slug).write_text(json.dumps(binding), encoding="utf-8")
        with _patched_config([invalid_name], default=invalid_name):
            async with TestClient(TestServer(_make_members_app(state))) as client:
                response = await client.post(f"/api/members/{slug}/thread")
                assert response.status == 409
                assert (await response.json())["code"] == "member_pin_mismatch"
        assert member_slot_key(slug) not in state._slots
        assert read_dm_binding(slug)["member"] == invalid_name

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

    def test_non_dispatchable_member_identity_is_skipped(self):
        from kiro_crew.dashboard.chat_persistence import (
            _SKIP_MEMBER_RESTORE,
            _member_restore_identity,
        )

        name = "crew password=shortvalue"
        slug = slug_for_name(name)
        key = member_slot_key(slug)
        write_dm_binding(slug, member=name, slot_key=key)

        assert _member_restore_identity(key) is _SKIP_MEMBER_RESTORE

    def test_skipped_restore_warning_does_not_log_a_credential_shaped_slot(self, caplog):
        import logging

        from kiro_crew.dashboard.chat_persistence import (
            _SKIP_MEMBER_RESTORE,
            _member_restore_identity,
        )

        name = "xoxb-000000000000-abcdefghijkl"
        slug = slug_for_name(name)
        key = member_slot_key(slug)
        write_dm_binding(slug, member=name, slot_key=key)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_persistence"):
            assert _member_restore_identity(key) is _SKIP_MEMBER_RESTORE

        messages = [record.getMessage() for record in caplog.records]
        assert any("no dispatchable dm binding" in message for message in messages)
        assert not any(name in message for message in messages)
        assert not any(slug in message for message in messages)

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

        A constructor reservation that refuses a bare member key before the
        binding is consulted breaks this; binding-first resolution is what
        admits the legitimate restore.
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

    @pytest.mark.asyncio
    async def test_resume_refuses_a_non_dispatchable_member_binding(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        name = "crew password=shortvalue"
        slug = slug_for_name(name)
        slot_key = member_slot_key(slug)
        write_dm_binding(slug, member=name, slot_key=slot_key)
        history_key = f"dashboard:{slot_key}"
        state.conversation_log.append(history_key, "user", "hello")
        state.conversation_log.update_metadata(
            history_key,
            {
                "agent": name,
                "mode": DM_SLOT_MODE,
                "closed": True,
                "closed_at": time.time() - 60,
            },
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                f"/api/chat/slots/{slot_key}/resume", json={"key": history_key}
            )
            assert response.status == 409
            body = await response.json()

        assert body["code"] == "member_pin_mismatch"
        assert slot_key not in state._slots
        metadata = state.conversation_log.get_metadata(history_key)
        assert metadata["closed"] is True
        assert "closed_at" in metadata

    @pytest.mark.asyncio
    async def test_resume_refuses_a_live_non_dispatchable_member_slot(self, tmp_path):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        name = "crew password=shortvalue"
        slug = slug_for_name(name)
        slot = _member_slot(state, key=member_slot_key(slug), agent=name)
        history_key = f"dashboard:{slot.key}"

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/ordinary-alias/resume", json={"key": history_key}
            )
            assert response.status == 409
            body = await response.json()

        assert body["code"] == "member_pin_mismatch"
        assert state._slots[slot.key] is slot

    @pytest.mark.asyncio
    async def test_resume_rechecks_member_dispatchability_after_binding_await(
        self, tmp_path, monkeypatch
    ):
        from chat_test_helpers import _make_app

        state = _make_state(tmp_path)
        slot_key = member_slot_key("dr-eggbot")
        history_key = f"dashboard:{slot_key}"
        state.conversation_log.append(history_key, "user", "hello")
        state.conversation_log.update_metadata(
            history_key, {"agent": "dr. eggbot", "mode": DM_SLOT_MODE}
        )
        binding_reads = iter(
            [
                {"member": "dr. eggbot"},
                {"member": "crew password=shortvalue"},
            ]
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.members_mod.read_dm_binding_for_slot",
            lambda _slot_key: next(binding_reads),
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                f"/api/chat/slots/{slot_key}/resume", json={"key": history_key}
            )
            assert response.status == 409
            body = await response.json()

        assert body["code"] == "member_pin_mismatch"
        assert slot_key not in state._slots


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
