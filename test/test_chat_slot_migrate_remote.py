"""Tests for POST /api/chat/slots/{slot}/migrate-remote (context-carry v1).

The migration re-homes a conversation: the FULL transcript travels to the chosen
crew through the shipped session-transfer bundle, a new local slot is bound to
the key the peer returns, and the source is archived read-only LAST with a
``migrated`` stamp written by that confirmed close. The tests here pin the
guard surface, the bundle hand-off, the failure ordering (a mid-flight failure
must leave the source intact and addressable), the pop-to-commit window, and
the archived source's read-only resume.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import (
    SlotCloseError,
    api_chat_slot_migrate_remote,
    api_chat_slot_resume,
    migrated_key_mint_decision,
)
from kiro_crew.dashboard.chat_utils import effective_session_key


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/migrate-remote", api_chat_slot_migrate_remote)
    app.router.add_post("/api/chat/slots/{slot}/resume", api_chat_slot_resume)
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_post("/api/chat", chat_handlers.api_chat)
    from kiro_crew.dashboard import openai_compat

    app.router.add_post("/v1/chat/completions", openai_compat.api_completions)
    return app


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """Every test runs as the local dashboard owner unless it opts out."""
    # Both binding sites: the migrate handler holds a module-scope binding in
    # chat_handlers; other handlers import from source_providers at call time.
    monkeypatch.setattr(chat_handlers, "is_owner_dashboard_request", lambda request: True)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _seed_conversation(slot, turns: int = 3) -> None:
    for i in range(turns):
        slot.messages.append({"role": "user", "content": f"question {i}"})
        slot.messages.append({"role": "assistant", "content": f"answer {i}"})


def _stub_transfer(state, monkeypatch, key: str = "peer-key-9"):
    """Install a fake transfer path: the bundle builder and the tunnel send.

    Returns the manager mock so tests can assert on ``send_session_bundle``.
    """
    mgr = MagicMock()
    mgr.send_session_bundle = AsyncMock(return_value=(True, {"key": key}))
    state.instances_manager = mgr
    monkeypatch.setattr(
        chat_handlers,
        "build_transfer_bundle_async",
        AsyncMock(return_value={"messages": [{"role": "user", "content": "q"}]}),
    )
    monkeypatch.setattr(chat_handlers, "local_instance_label", lambda: "local")
    return mgr


class TestGuards:
    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/nope/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_missing_instance_id_is_400(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/migrate-remote", json={})
            assert resp.status == 400
            assert (await resp.json())["code"] == "migrate_instance_required"

    @pytest.mark.asyncio
    async def test_app_token_is_refused_as_404(self, tmp_path):
        """An app token gets the same 404 shape as a missing slot: no oracle."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        @web.middleware
        async def _stamp_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(_stamp_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        assert "s1" in state._slots  # nothing was touched

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        monkeypatch.setattr(chat_handlers, "is_owner_dashboard_request", lambda request: False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 403
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_already_remote_is_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.executor = "remote"
        slot.instance_id = "crew-a"
        slot.remote_slot = "peer-1"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-b"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_already_remote"

    @pytest.mark.asyncio
    async def test_member_thread_is_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.mode = "member"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_member_pinned"

    @pytest.mark.asyncio
    async def test_running_turn_is_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        # ``running`` is derived from the turn task, so stage a live one.
        slot.task = MagicMock(done=lambda: False)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_peer_transfer_refusal_is_502_and_source_intact(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        mgr = _stub_transfer(state, monkeypatch)
        mgr.send_session_bundle = AsyncMock(
            return_value=(False, {"error": "peer refused", "code": "transfer_peer_refused"})
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "transfer_peer_refused"
        assert "s1" in state._slots
        assert len(state._slots) == 1  # no orphaned new slot
        assert state._slots["s1"]._migrating is False


class TestSuccess:
    @pytest.mark.asyncio
    async def test_migrates_context_and_archives_source(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", agent="oncall")
        _seed_conversation(slot, turns=3)
        mgr = _stub_transfer(state, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["instance_id"] == "crew-a"
            new_key = data["key"]

        # The source is archived (gone from the live map); the new slot is
        # bound to the chosen crew.
        assert "s1" not in state._slots
        new_slot = state._slots[new_key]
        assert new_slot.executor == "remote"
        assert new_slot.instance_id == "crew-a"
        assert new_slot.remote_slot == "peer-key-9"
        # Execution settings are NOT copied: the peer runs its own defaults, so
        # stamping the source's agent on the mirror would display settings that
        # are not the ones actually running.
        assert new_slot.agent == ""

        # The FULL conversation traveled through the shipped transfer-bundle
        # path — the peer imports the real transcript and the mirror binds to
        # the key it returns. Nothing rides the local pending-context queue.
        mgr.send_session_bundle.assert_awaited_once()
        assert mgr.send_session_bundle.await_args.args[0] == "crew-a"
        assert list(new_slot._pending_context) == []

        # The archived source's persisted metadata points at where the work
        # went, so the History row can render the affordance after a restart.
        meta = state.conversation_log.get_metadata("dashboard:s1") or {}
        assert meta.get("closed") is True
        assert meta.get("migrated") == {
            "instance_id": "crew-a",
            "remote_key": "peer-key-9",
        }

    @pytest.mark.asyncio
    async def test_fresh_session_mints_an_empty_peer_slot_instead_of_a_bundle(
        self, tmp_path, monkeypatch
    ):
        """The importer refuses an empty bundle by design, so a session with no
        messages must take the create_peer_slot path — and never call send."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        mgr = _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers, "build_transfer_bundle_async", AsyncMock(return_value={"messages": []})
        )
        created = AsyncMock(return_value="peer-fresh-1")
        monkeypatch.setattr(chat_handlers, "create_peer_slot", created)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert resp.status == 200
        mgr.send_session_bundle.assert_not_awaited()
        created.assert_awaited_once()
        assert state._slots[data["key"]].remote_slot == "peer-fresh-1"

    @pytest.mark.asyncio
    async def test_archived_source_resume_is_read_only(self, tmp_path, monkeypatch):
        """Resuming a migrated source is refused: the session lives on the crew."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "resume_migrated"
            assert data["migrated"]["instance_id"] == "crew-a"
        assert "s1" not in state._slots  # the refusal did not reanimate it

    @pytest.mark.asyncio
    async def test_a_send_or_create_on_the_archived_key_does_not_mint_a_fork(
        self, tmp_path, monkeypatch
    ):
        """Every request-named creation path mints when the name is absent
        from the live set; a migrated source is exactly that. Chat send and
        slot create must both refuse before get_or_create_slot runs."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hello?"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "resume_migrated"
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "resume_migrated"
            # The OpenAI-compatible endpoint lets its caller NAME the slot via
            # `id` — the same mint, so the same decision, in its own envelope.
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "id": "s1",
                    "model": "default",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "resume_migrated"
            assert body["error"]["code"] == "resume_migrated"
            assert body["migrated"]["instance_id"] == "crew-a"
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_an_unreadable_record_refuses_the_mint_instead_of_forking(
        self, tmp_path, monkeypatch
    ):
        """`get_metadata` folds "exists but unreadable" (a Windows sharing
        violation, a torn write) into "absent", and an absent stamp mints a
        writable local fork of a session that lives on the crew. The mint must
        fail CLOSED on the status form: 503 and retryable, nothing created."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            # The archived record is now on disk with its migrated stamp; make
            # it unreadable the way the log itself reports the condition.
            real = state.conversation_log.get_metadata_status

            def _unreadable(key):
                meta, _readable = real(key)
                return ({} if key == "dashboard:s1" else meta), key != "dashboard:s1"

            monkeypatch.setattr(state.conversation_log, "get_metadata_status", _unreadable)
            for path, body in (
                ("/api/chat", {"slot": "s1", "message": "hello?"}),
                ("/api/chat/slots", {"name": "s1"}),
            ):
                resp = await client.post(path, json=body)
                assert (resp.status, (await resp.json())["code"]) == (503, "history_unreadable")
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "id": "s1",
                    "model": "default",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status == 503
            body = await resp.json()
            assert body["code"] == "history_unreadable"
            assert body["error"]["code"] == "history_unreadable"
            assert body["error"]["type"] == "service_unavailable_error"
        assert "s1" not in state._slots  # no fork was minted by any path

    @pytest.mark.asyncio
    async def test_an_unreadable_record_is_oracle_free_for_an_app_caller(
        self, tmp_path, monkeypatch
    ):
        """An app token cannot have its ownership verified against a record that
        will not read, and must not learn from the refusal that the key exists:
        it gets the same 404 an unknown key gets."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        monkeypatch.setattr(
            state.conversation_log, "get_metadata_status", lambda key: ({}, key != "dashboard:s1")
        )
        decision = migrated_key_mint_decision(state, "some-app", "s1")
        assert decision is not None and decision[0] == "slot_not_found"
        decision = migrated_key_mint_decision(state, "", "s1")
        assert decision is not None and decision[0] == "history_unreadable"

    @pytest.mark.asyncio
    async def test_a_refused_create_never_mints_a_peer_session_first(self, tmp_path, monkeypatch):
        """A create addressed to a migrated key WITH an instance_id is refused
        before `create_peer_slot` runs: the refusal depends only on the key and
        the caller, and a 409 landing after the peer mint would leave a session
        on the crew that nothing local is bound to."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            created = AsyncMock(return_value="peer-key-orphan")
            monkeypatch.setattr(chat_handlers, "create_peer_slot", created)
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "instance_id": "crew-b"}
            )
            assert (resp.status, (await resp.json())["code"]) == (409, "resume_migrated")
        created.assert_not_called()
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spelling", ["dashboard:s1", "dashboard_s1"])
    async def test_an_aliased_resume_of_the_migrating_source_is_refused(
        self, tmp_path, monkeypatch, spelling
    ):
        """A resume names two keys: the slot it rehydrates INTO and the
        conversation it rehydrates FROM. Between the source's pop and its
        durable stamp only the RESERVATION knows the key is spoken for, and a
        gate that checks the reservation against the target slot alone lets
        ``POST /slots/s2/resume {key: dashboard:s1}`` in that window fork s1's
        transcript into a writable s2 while s1 is leaving for the crew. Both
        keys go through the centralized decision, reservation included — and
        the source key is derived by the shared fold, so the ``dashboard_``
        filename-stem spelling (which reads the same transcript) is guarded
        exactly like the ``dashboard:`` session-key spelling."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        # Persist the transcript, then model the window: source popped, no
        # stamp written, reservation held for the owner.
        state.conversation_log.append("dashboard:s1", "user", "question 0")
        state.conversation_log.append("dashboard:s1", "assistant", "answer 0")
        state._slots.pop("s1")
        state._migrating_keys["s1"] = ""
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s2/resume", json={"key": spelling})
            assert (resp.status, (await resp.json())["code"]) == (409, "migrate_in_flight")
        assert "s2" not in state._slots

    @pytest.mark.asyncio
    async def test_a_resume_fails_closed_on_an_unreadable_record(self, tmp_path, monkeypatch):
        """`get_metadata` reports an unreadable record as empty, so a resume
        gate that re-derives the migrated stamp from it fails open — the last
        mint path to do so once send/create are hardened. The resume runs the
        same fail-closed decision as every other mint."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            monkeypatch.setattr(
                state.conversation_log,
                "get_metadata_status",
                lambda key: ({}, key != "dashboard:s1"),
            )
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert (resp.status, (await resp.json())["code"]) == (503, "history_unreadable")
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_a_display_edit_during_the_peer_round_trip_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        """A rename, tag edit or folder move mutates neither the transcript
        anchors nor the sid. The destination is stamped with the source's
        display fields BEFORE the persist/archive awaits, so an edit landing in
        that window would be stranded on the read-only source while the crew
        carries the stale copy. The display fields are the fourth anchor."""
        for mutate in (
            lambda s: setattr(s, "title", "renamed during migration"),
            lambda s: s.tags.append("late-tag"),
            lambda s: setattr(s, "folder_id", "folder-moved-into"),
        ):
            state = _make_state(tmp_path)
            slot = state.get_or_create_slot("s1")
            _seed_conversation(slot)
            slot.title, slot._titled = "original", True
            _stub_transfer(state, monkeypatch)
            before = (len(slot.messages), slot._dirty_gen)
            # The destination persist is the first await AFTER the stamp: the
            # edit lands there, past the copy and before the archive.
            real_update = state.conversation_log.update_metadata

            def _edit_source_during_dest_persist(key, meta, _m=mutate, _real=real_update):
                if key != "dashboard:s1":
                    _m(slot)
                return _real(key, meta)

            monkeypatch.setattr(
                state.conversation_log, "update_metadata", _edit_source_during_dest_persist
            )
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "turn_in_flight"
            assert (len(slot.messages), slot._dirty_gen) == before  # transcript anchors blind
            assert "s1" in state._slots
            assert state._slots["s1"].migrated is None
            assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_a_degraded_import_refuses_to_archive_the_source(self, tmp_path, monkeypatch):
        """The import reports its fidelity. A bundle that CARRIED kiro-cli
        context and came back ``resume_mode: prefix`` means the peer holds the
        transcript and lost the native context to an ordinary IO fault on its
        side. The mirror path tolerates that with a live source and a
        "transcript only" row; a migration would archive the only full copy
        read-only. It refuses instead, before any local mirror exists."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "build_transfer_bundle_async",
            AsyncMock(
                return_value={
                    "messages": [{"role": "user", "content": "q"}],
                    "layer_b": {"envelope": {}, "events": []},
                }
            ),
        )
        mgr.send_session_bundle = AsyncMock(
            return_value=(True, {"key": "peer-key-9", "resume_mode": "prefix"})
        )
        live_before = set(state._slots)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (502, "migrate_degraded_import")
        assert set(state._slots) == live_before  # no mirror was minted
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False
        # Full fidelity with the same bundle proceeds.
        mgr.send_session_bundle = AsyncMock(
            return_value=(True, {"key": "peer-key-9", "resume_mode": "session_load"})
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_a_sender_side_skipped_context_refuses_before_sending(
        self, tmp_path, monkeypatch
    ):
        """`layer_b_skipped` is the builder saying the session HAD context and is
        not carrying it (size cap, unreadable files, withheld destination). The
        peer would import a transcript-only copy and the source would be archived
        read-only with the only copy of the context — so the migration refuses
        before the send, the sender-side twin of the post-import fidelity gate."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "build_transfer_bundle_async",
            AsyncMock(
                return_value={
                    "messages": [{"role": "user", "content": "q"}],
                    "layer_b_skipped": True,
                }
            ),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (502, "migrate_degraded_import")
        mgr.send_session_bundle.assert_not_called()  # the peer never saw it
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_a_transcript_only_bundle_is_not_degraded(self, tmp_path, monkeypatch):
        """``prefix`` is the CORRECT fidelity for a bundle that never carried
        context (a session with none, or a v1 peer); refusing it would block
        every plain migration."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)  # bundle has no layer_b
        mgr.send_session_bundle = AsyncMock(
            return_value=(True, {"key": "peer-key-9", "resume_mode": "prefix"})
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_a_rollback_pop_publishes_the_slot_list(self, tmp_path, monkeypatch):
        """Creation broadcasts the destination to every client. A rollback that
        pops it silently leaves a sidebar tab for a slot absent from the backend
        until some unrelated push; the pop is published like the create."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)

        async def _archive_fails(state_, src, name, *, pre_pop_check=None, migrated=None):
            raise SlotCloseError("disk full", code="close_persist_failed")

        monkeypatch.setattr(chat_handlers, "close_slot", _archive_fails)
        seen: list[set[str]] = []
        state.push_slots_update = lambda: seen.append(set(state._slots))  # type: ignore[method-assign]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (500, "close_persist_failed")
        assert set(state._slots) == {"s1"}  # destination popped
        # The create's push saw the destination; the LAST push must not, or a
        # client applying it keeps the tab.
        assert any(len(s) == 2 for s in seen)
        assert seen[-1] == {"s1"}

    @pytest.mark.asyncio
    async def test_an_armed_nudge_loop_refuses_the_migration(self, tmp_path, monkeypatch):
        """An armed auto-nudge / monitor loop idles between cycles with
        ``running`` False and an empty queue, so the turn guard cannot see it,
        and archiving the source retires the loop while the destination carries
        no execution settings: the scheduled work would stop silently. Refused
        at entry, and at the pre-pop re-check when the loop is armed inside the
        peer round-trip."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)
        armed: dict[str, object] = {}
        monkeypatch.setattr(chat_handlers, "_slot_nudge_loop", lambda name: armed.get(name))
        # Entry: loop already armed.
        armed["s1"] = object()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (409, "migrate_nudge_loop_armed")
        mgr.send_session_bundle.assert_not_called()
        assert set(state._slots) == {"s1"}
        # Round-trip: armed while the bundle is in flight.
        armed.clear()

        async def _arm_during_send(instance_id, bundle):
            armed["s1"] = object()
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _arm_during_send
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (409, "migrate_nudge_loop_armed")
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False
        # No armed loop: the same slot migrates.
        armed.clear()
        mgr.send_session_bundle = AsyncMock(return_value=(True, {"key": "peer-key-9"}))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_a_loop_armed_in_the_round_trip_is_refused_at_the_commit_not_retired(
        self, tmp_path, monkeypatch
    ):
        """`close_slot` retires the source's loop BEFORE its pre-pop re-check, so
        a registry lookup at the commit point reads empty and a loop armed during
        the peer round-trip would be retired for good. The re-check receives the
        retired loop from `close_slot` and refuses on it; the refusal makes
        `close_slot` restore the loop."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)
        registry: dict[str, object] = {}
        # Entry sees no loop; the loop is armed inside the tunnel send.
        monkeypatch.setattr(chat_handlers, "_slot_nudge_loop", lambda name: registry.get(name))

        async def _arm_during_send(instance_id, bundle):
            registry["s1"] = "loop-armed-mid-transfer"
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _arm_during_send
        restored: list[object] = []

        async def _retire(name):
            return registry.pop(name, None)  # the real one empties the registry too

        async def _restore(loop, admission_check):
            restored.append(loop)
            registry["s1"] = loop

        monkeypatch.setattr(chat_handlers, "_retire_slot_nudge_loop", _retire)
        monkeypatch.setattr(chat_handlers, "_restore_slot_nudge_loop", _restore)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (409, "migrate_nudge_loop_armed")
        assert restored == ["loop-armed-mid-transfer"]
        assert registry.get("s1") == "loop-armed-mid-transfer"  # back where it was
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_a_change_inside_the_bundle_build_is_refused_before_sending(
        self, tmp_path, monkeypatch
    ):
        """The bundle build is an await. Anchors taken AFTER it would capture a
        reset (or a completed turn) that landed inside it as the baseline, and
        the commit-point check would then see no change on a source whose live
        state differs from what the bundle carries. Anchors are taken before
        the build and re-checked right after it: a change inside the build is
        refused before anything reaches the peer."""
        for mutate in (
            lambda s, live: live.__setitem__("sid", None),  # a reset
            lambda s, live: s.messages.append({"role": "user", "content": "raced"}),
        ):
            state = _make_state(tmp_path)
            slot = state.get_or_create_slot("s1")
            _seed_conversation(slot)
            mgr = _stub_transfer(state, monkeypatch)
            live = {"sid": "sid-before"}
            state.sessions.resumable_sid = lambda key: live["sid"]  # type: ignore[method-assign]

            async def _build_while_source_moves(state_, slot_, *, origin, _m=mutate):
                _m(slot_, live)
                return {"messages": [{"role": "user", "content": "q"}]}

            monkeypatch.setattr(
                chat_handlers, "build_transfer_bundle_async", _build_while_source_moves
            )
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
                )
                assert (resp.status, (await resp.json())["code"]) == (409, "turn_in_flight")
            mgr.send_session_bundle.assert_not_called()  # nothing reached the peer
            assert "s1" in state._slots
            assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_a_private_memory_binding_refuses_the_migration(self, tmp_path, monkeypatch):
        """An ordinary chat (``mode == ""``) can carry a private V2 memory
        binding — the owner-selected member pin writes one on the first turn —
        and the bundle cannot carry it. Bound: refused before anything is sent.
        Binding record present but unreadable: the same refusal, not
        permission. Bound inside the peer round-trip: refused at the commit
        point. Unbound: migrates."""
        from kiro_crew import member_memory_auth

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)
        binding: dict[str, object] = {}

        def _read(session_key):
            value = binding.get(session_key)
            if value == "unreadable":
                raise ValueError("The protected member session binding is missing or unreadable")
            return value

        monkeypatch.setattr(member_memory_auth, "read_private_session_store", _read)
        key = effective_session_key(slot)
        for value in ("member-store-a", "unreadable"):
            binding[key] = value
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
                )
                assert (resp.status, (await resp.json())["code"]) == (
                    409,
                    "migrate_private_memory_bound",
                )
            mgr.send_session_bundle.assert_not_called()
        # Bound inside the round-trip.
        binding.clear()

        async def _bind_during_send(instance_id, bundle):
            binding[key] = "member-store-a"
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _bind_during_send
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert (resp.status, (await resp.json())["code"]) == (
                409,
                "migrate_private_memory_bound",
            )
        assert "s1" in state._slots and state._slots["s1"].migrated is None
        # Unbound: migrates.
        binding.clear()
        mgr.send_session_bundle = AsyncMock(return_value=(True, {"key": "peer-key-9"}))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_an_app_token_mint_on_the_archived_key_gets_the_oracle_free_404(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200

        @web.middleware
        async def _stamp_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app2 = _make_app(state)
        app2.middlewares.append(_stamp_app)
        async with TestClient(TestServer(app2)) as client:
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 404
            assert "migrated" not in (await resp.json())
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "id": "s1",
                    "model": "default",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status == 404
            assert "migrated" not in (await resp.json())
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_app_token_resume_of_migrated_key_gets_the_oracle_free_404(
        self, tmp_path, monkeypatch
    ):
        """The migrated 409 discloses the peer binding (instance_id +
        remote_key); an app token that does not own the persisted conversation
        must get the same 404 an unknown key would, BEFORE that disclosure."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)

        @web.middleware
        async def _stamp_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        app2 = _make_app(state)
        app2.middlewares.append(_stamp_app)
        async with TestClient(TestServer(app2)) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 404
            body = await resp.json()
            assert "migrated" not in body, "peer identifiers leaked to a non-owning app"

    @pytest.mark.asyncio
    async def test_a_running_workflow_on_the_session_refuses_migration(self, tmp_path, monkeypatch):
        """A background workflow injects its result back by session_key without
        passing through any endpoint — archiving the source mid-run strands the
        completion. Same boundary as the sub-agent gate."""
        from types import SimpleNamespace

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        state.workflow_service = SimpleNamespace(
            registry=SimpleNamespace(
                _runs={
                    "r1": SimpleNamespace(
                        session_key=chat_handlers.effective_session_key(slot),
                        status="running",
                    )
                }
            )
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_workflow_active"
        assert "s1" in state._slots
        # A FINISHED run on the same session does not block.
        state.workflow_service.registry._runs["r1"] = SimpleNamespace(
            session_key=chat_handlers.effective_session_key(slot), status="finished"
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_the_granted_owner_decision_is_audited_before_fallible_arms(
        self, tmp_path, monkeypatch
    ):
        """An authorized request that then fails on the manager arm must still
        leave the allowed decision in the audit trail."""
        from unittest.mock import MagicMock

        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.instances_manager = None  # force the earliest fallible arm
        _sel = MagicMock()
        monkeypatch.setattr(chat_handlers, "sel", lambda: _sel)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status in (500, 502, 503)
        allowed = [
            c
            for c in _sel.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "allowed"
            and c.kwargs.get("operation") == "chat.slot_migrate_remote"
        ]
        assert allowed, "owner authorization was granted but never audited"


class TestFailureOrdering:
    @pytest.mark.asyncio
    async def test_a_destination_that_received_work_survives_a_failed_archive(
        self, tmp_path, monkeypatch
    ):
        """The destination is published at creation; a turn landing on it
        during the archive window must not be discarded by the rollback."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)

        async def _close_after_dest_gets_a_turn(
            state_, src, name, *, pre_pop_check=None, migrated=None
        ):
            dest = next(s for k, s in state_._slots.items() if k != "s1")
            dest.messages.append({"role": "user", "content": "landed mid-migration"})
            raise SlotCloseError("disk full", "close_persist_failed")

        monkeypatch.setattr(chat_handlers, "close_slot", _close_after_dest_gets_a_turn)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "migrate_split"
        assert "s1" in state._slots and state._slots["s1"].migrated is None
        dest = state._slots[data["key"]]
        assert dest.messages[-1]["content"] == "landed mid-migration"

    @pytest.mark.asyncio
    async def test_a_destination_that_accepted_context_survives_a_failed_archive(
        self, tmp_path, monkeypatch
    ):
        """``/context`` on the published destination answers ok and parks the
        payload for the next turn -- no message, no running turn, no queue. The
        rollback must still treat that as work: popping the slot would silently
        discard an acknowledged delivery."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)

        async def _close_after_dest_accepts_context(
            state_, src, name, *, pre_pop_check=None, migrated=None
        ):
            dest = next(s for k, s in state_._slots.items() if k != "s1")
            dest.append_pending_context({"kind": "note", "text": "accepted mid-migration"})
            assert not dest.messages and not dest.running and dest.queue_depth == 0
            raise SlotCloseError("disk full", "close_persist_failed")

        monkeypatch.setattr(chat_handlers, "close_slot", _close_after_dest_accepts_context)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "migrate_split"
        assert "s1" in state._slots and state._slots["s1"].migrated is None
        dest = state._slots[data["key"]]
        assert (
            dest._pending_context and dest._pending_context[-1]["text"] == "accepted mid-migration"
        )

    @pytest.mark.asyncio
    async def test_a_destination_that_received_work_survives_a_failed_persist(
        self, tmp_path, monkeypatch
    ):
        """The persist-failure rollback has the same window as the archive one:
        `update_metadata` runs in a thread, and a turn can land on the published
        destination meanwhile. It must be kept, not popped."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)
        real_update = state.conversation_log.update_metadata

        def _turn_lands_then_write_fails(history_key, fields):
            if fields.get("executor") == "remote":
                dest = next(s for k, s in state._slots.items() if k != "s1")
                dest.messages.append({"role": "user", "content": "landed during persist"})
                raise OSError("disk full")
            return real_update(history_key, fields)

        monkeypatch.setattr(state.conversation_log, "update_metadata", _turn_lands_then_write_fails)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert (resp.status, data["code"]) == (409, "migrate_split")
        # Both live; the destination keeps its turn and is dirty so the flush
        # retries the binding write that failed.
        assert "s1" in state._slots
        dest = state._slots[data["key"]]
        assert dest.messages[-1]["content"] == "landed during persist" and dest._dirty

    @pytest.mark.asyncio
    async def test_a_destination_that_received_work_during_its_closed_save_is_kept(
        self, tmp_path, monkeypatch
    ):
        """Third rollback pop: after the archive fails, the destination's own
        closed save is an await too. Work landing THERE is kept — the slot is
        left live and dirty (the flush rewrites it open), not popped."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "close_slot",
            AsyncMock(side_effect=SlotCloseError("disk full", "close_persist_failed")),
        )

        async def _closed_save_with_a_turn_landing(state_, slot_, *a, **kw):
            if kw.get("closed"):
                slot_.messages.append({"role": "user", "content": "landed during closed save"})
            return True

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _closed_save_with_a_turn_landing)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert (resp.status, data["code"]) == (409, "migrate_split")
        dest = state._slots[data["key"]]
        assert dest.messages[-1]["content"] == "landed during closed save" and dest._dirty

    @pytest.mark.asyncio
    async def test_archive_failure_rolls_back_the_new_slot(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "close_slot",
            AsyncMock(side_effect=SlotCloseError("disk full", "close_persist_failed")),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
            assert (await resp.json())["code"] == "close_persist_failed"
        assert "s1" in state._slots
        assert len(state._slots) == 1
        # The speculative stamp was cleared, so a later ordinary save cannot
        # persist a pointer at a slot that was rolled back.
        assert state._slots["s1"].migrated is None


class TestArchiveWindow:
    """The two windows around ``close_slot``: the key is popped before the
    closed+migrated stamp is durable, and a periodic flush can run while the
    close awaits. Neither may mint a fork or poison a still-live source."""

    @pytest.mark.asyncio
    async def test_send_and_resume_inside_the_pop_to_commit_window_are_refused(
        self, tmp_path, monkeypatch
    ):
        """While the source is popped but not yet stamped, a send, a create and a
        resume on the old key are refused (409 migrate_in_flight) and mint no
        replacement — the reservation covers what neither the live map nor the
        disk stamp can see yet. After the commit the reservation is gone and the
        persisted stamp takes over."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", agent="oncall")
        _seed_conversation(slot, turns=2)
        _stub_transfer(state, monkeypatch)
        real_save = chat_handlers.save_slot_off_loop
        observed: dict[str, object] = {}

        async def _save_probing_the_window(state_, slot_, *a, **kw):
            if kw.get("closed") and slot_ is slot:
                # The source is out of the live map; the stamp is not on disk.
                assert "s1" not in state_._slots
                assert not (state_.conversation_log.get_metadata("dashboard:s1") or {}).get(
                    "migrated"
                )
                async with TestClient(TestServer(_make_app(state_))) as client:
                    r1 = await client.post("/api/chat", json={"slot": "s1", "message": "x"})
                    r2 = await client.post("/api/chat/slots", json={"name": "s1"})
                    r3 = await client.post(
                        "/api/chat/slots/s1/resume", json={"key": "dashboard:s1"}
                    )
                    observed["codes"] = [
                        (r.status, (await r.json()).get("code")) for r in (r1, r2, r3)
                    ]
                observed["minted"] = "s1" in state_._slots
            return await real_save(state_, slot_, *a, **kw)

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save_probing_the_window)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            # After the commit: reservation released, disk stamp in force.
            assert "s1" not in state._migrating_keys
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "x"})
            assert (resp.status, (await resp.json())["code"]) == (409, "resume_migrated")
        assert observed["codes"] == [(409, "migrate_in_flight")] * 3
        assert observed["minted"] is False
        assert "s1" not in state._slots

    @pytest.mark.asyncio
    async def test_reservation_is_released_on_every_exit(self, tmp_path, monkeypatch):
        """A failed archive AND an unexpected exception both release the key, so
        the source stays addressable afterwards."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", agent="oncall")
        _seed_conversation(slot, turns=2)
        _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "close_slot",
            AsyncMock(side_effect=SlotCloseError("disk full", "close_persist_failed")),
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
            assert "s1" not in state._migrating_keys and slot._migrating is False
            monkeypatch.setattr(
                chat_handlers, "close_slot", AsyncMock(side_effect=RuntimeError("boom"))
            )
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
            assert "s1" not in state._migrating_keys and slot._migrating is False
            # Addressable afterwards: a send is admitted (no 409). This starts a
            # real turn on s1, so it is the LAST step -- a migrate racing that
            # turn is correctly refused as turn_in_flight, which is not what this
            # test is about.
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "still here"})
            assert resp.status != 409

    @pytest.mark.asyncio
    async def test_a_flush_during_the_close_never_persists_a_speculative_stamp(
        self, tmp_path, monkeypatch
    ):
        """A periodic save that runs while the close awaits writes NO migrated
        marker (the stamp travels only with the confirmed closed save), so when
        the close then aborts the still-live source is not poisoned: its disk
        record carries no ``migrated`` and a resume of it is not refused."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1", agent="oncall")
        _seed_conversation(slot, turns=2)
        _stub_transfer(state, monkeypatch)
        real_close = chat_handlers.close_slot

        async def _flush_then_abort(state_, slot_, name, *, pre_pop_check=None, migrated=None):
            # The flush the window admits: a plain save of the live slot.
            assert slot_.migrated is None, "stamp set on the live slot before the close"
            assert await chat_handlers.save_slot_off_loop(state_, slot_, force=True)
            raise SlotCloseError("disk full", "close_persist_failed")

        monkeypatch.setattr(chat_handlers, "close_slot", _flush_then_abort)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
        meta = state.conversation_log.get_metadata("dashboard:s1") or {}
        assert "migrated" not in meta and not meta.get("closed")
        assert "s1" in state._slots and state._slots["s1"].migrated is None
        # And the real close path DOES stamp — the argument, not the attribute.
        monkeypatch.setattr(chat_handlers, "close_slot", real_close)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
        meta = state.conversation_log.get_metadata("dashboard:s1") or {}
        assert meta.get("closed") is True
        assert meta.get("migrated", {}).get("instance_id") == "crew-a"


class TestReviewRoundHardening:
    """The migrate endpoint's adversarial and crash-ordering properties: the
    existence oracle stays closed for app tokens, a turn racing the archive
    aborts it, the destination binding is durable before the source locks,
    and a refused resume leaves the archive flag standing."""

    @pytest.mark.asyncio
    async def test_app_token_gets_the_same_404_for_existing_and_absent_slots(self, tmp_path):
        """The denial runs before the slot lookup AND before body validation,
        so an app token probing with an empty body cannot tell an existing
        slot (400 would leak) from an absent one."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        @web.middleware
        async def _stamp_app(request, handler):
            request["app"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.append(_stamp_app)
        async with TestClient(TestServer(app)) as client:
            shapes = []
            for slot_name in ("s1", "absent"):
                resp = await client.post(f"/api/chat/slots/{slot_name}/migrate-remote", json={})
                shapes.append((resp.status, (await resp.json())["code"]))
            assert shapes[0] == shapes[1] == (404, "slot_not_found")

    @pytest.mark.asyncio
    async def test_a_turn_starting_mid_migration_aborts_the_archive(self, tmp_path, monkeypatch):
        """The peer round-trips are suspension points where a message can start
        a turn the guard never saw; close_slot's pre-pop revalidation must
        refuse rather than archive the running turn uncarried."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)

        mgr = _stub_transfer(state, monkeypatch)

        async def _send_and_start_turn(instance_id, bundle):
            # A message lands while the migrate handler awaits the peer: a turn
            # starts AND appends to the transcript inside the tunnel send.
            slot.task = MagicMock(done=lambda: False)
            slot.messages.append({"role": "user", "content": "raced message"})
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_and_start_turn
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        # The source survives, un-stamped and unlocked for a retry.
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_a_second_concurrent_migration_is_refused(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._migrating = True  # a migration already holds the slot
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_in_flight"

    @pytest.mark.asyncio
    async def test_the_destination_is_durably_persisted_before_the_archive(
        self, tmp_path, monkeypatch
    ):
        """A restart after migration must find the destination binding on disk —
        persist-then-archive means a crash at any point leaves at least one
        addressable copy of the conversation."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            data = await resp.json()
            assert resp.status == 200
        new_key = data["key"]
        meta = state.conversation_log.get_metadata(f"dashboard:{new_key}") or {}
        assert meta, "destination has no durable metadata"
        assert meta.get("closed") is not True

    @pytest.mark.asyncio
    async def test_a_refused_resume_does_not_clear_the_archive_flag(self, tmp_path, monkeypatch):
        """The migrated gate runs BEFORE clear_closed: a refused resume must
        leave the closed flag standing, or the next restart reanimates a
        writable local fork of a conversation that lives on the crew."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        _stub_transfer(state, monkeypatch)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 200
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 409
            assert (await resp.json())["code"] == "resume_migrated"
        meta = state.conversation_log.get_metadata("dashboard:s1") or {}
        assert meta.get("closed") is True, "resume durably dropped the archive flag"
        assert meta.get("migrated", {}).get("instance_id") == "crew-a"


class TestPeerEgressAndChannelBoundaries:
    """The digest is peer egress and must pass the standard non-user sinks;
    channel-linked sessions cannot be migrated; a failed rollback must not
    leave a self-reviving destination."""

    @pytest.mark.asyncio
    async def test_a_channel_origin_session_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.channel_origin = True
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_channel_linked"

    @pytest.mark.asyncio
    async def test_a_slack_linked_session_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.set_slack_link(effective_session_key(slot), "1700000000.1", "C123")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_channel_linked"

    @pytest.mark.asyncio
    async def test_an_app_owned_session_is_refused(self, tmp_path):
        """App runtimes address their slot by key and re-create it when gone —
        the same re-creation fork as a cron binding."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._app = "spec-builder"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_app_owned"
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_a_cron_linked_session_is_refused(self, tmp_path):
        """A persistent cron slot passes every other guard (not channel-origin,
        "cron:{id}" is not a channel namespace) but its driver re-creates the
        slot on the next run without consulting the migrated stamp — migrating
        one silently forks the conversation."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "cron:job-42"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_linked_session"
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None

    @pytest.mark.asyncio
    async def test_a_non_slack_mirror_linked_session_is_refused(self, tmp_path):
        """A Discord/Telegram mirror is a separate binding from the Slack link:
        an inbound-accepting mirror on a dashboard-shaped key passes
        channel_origin / is_channel_session_key / get_slack_link, and its
        channel keeps resolving turns onto this key after the archive."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_mirror_link = lambda key: object()  # type: ignore[method-assign]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_channel_linked"
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_a_mirror_link_landing_mid_transfer_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        """The peer round-trips are a window in which a channel mirror can be
        bound; the pre-pop revalidation must catch a link that landed inside."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        mgr = _stub_transfer(state, monkeypatch)
        _linked = {"now": False}
        state.sessions.get_mirror_link = (  # type: ignore[method-assign]
            lambda key: object() if _linked["now"] else None
        )

        async def _send_and_link(instance_id, bundle):
            _linked["now"] = True  # the mirror binds while the bundle is in flight
            return (True, {"key": "peer-key-7"})

        mgr.send_session_bundle = _send_and_link
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_channel_linked"
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None

    @pytest.mark.asyncio
    async def test_a_workflow_starting_mid_transfer_aborts_the_archive(self, tmp_path, monkeypatch):
        """The commit-point revalidation runs the SAME guard function as the
        entry check, so a workflow run that starts inside the peer round-trips
        is refused exactly as it would have been up front."""
        from types import SimpleNamespace

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot, turns=1)
        mgr = _stub_transfer(state, monkeypatch)
        _runs: dict = {}
        state.workflow_service = SimpleNamespace(registry=SimpleNamespace(_runs=_runs))

        async def _send_and_start_workflow(instance_id, bundle):
            _runs["r1"] = SimpleNamespace(
                session_key=chat_handlers.effective_session_key(slot), status="running"
            )
            return (True, {"key": "peer-key-8"})

        mgr.send_session_bundle = _send_and_start_workflow
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_workflow_active"
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None

    @pytest.mark.asyncio
    async def test_rollback_confirms_the_closed_write_before_popping(self, tmp_path, monkeypatch):
        """A best-effort save after the pop is swallowed on exactly the
        correlated fault that failed the archive; the destination must stay
        LIVE (and dirty) when its closed write cannot be confirmed, and the
        abandoned peer session is asked to close."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "close_slot",
            AsyncMock(side_effect=SlotCloseError("disk full", "close_persist_failed")),
        )
        # The correlated fault: the destination's closed write fails too.
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=False))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
        # Source intact; destination NOT popped (visible, dirty, retryable).
        # The peer is NOT closed on this arm: the live destination still holds
        # the binding, and closing its peer would break the surviving tab.
        assert "s1" in state._slots
        others = [k for k in state._slots if k != "s1"]
        assert len(others) == 1
        assert state._slots[others[0]]._dirty is True

    @pytest.mark.asyncio
    async def test_rollback_pops_the_destination_on_a_confirmed_closed_write(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        _stub_transfer(state, monkeypatch)
        monkeypatch.setattr(
            chat_handlers,
            "close_slot",
            AsyncMock(side_effect=SlotCloseError("disk full", "close_persist_failed")),
        )
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", AsyncMock(return_value=True))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 500
        assert list(state._slots.keys()) == ["s1"]

    @pytest.mark.asyncio
    async def test_a_queued_prompt_refuses_the_migration(self, tmp_path):
        """A queued prompt is ACCEPTED work; its answer must not land on a
        source the archive just locked read-only."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._queue.append({"content": "queued question"})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_a_non_plain_mode_refuses_the_migration(self, tmp_path):
        """Mode state (stage loop, crew store) lives outside the transcript;
        the plain-minted destination cannot run it and the archive strands it."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.mode = "orchestrator"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_mode_unsupported"

    @pytest.mark.asyncio
    async def test_a_mode_switch_during_the_peer_round_trip_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        """``mode`` is a live attribute that bumps neither the message count nor
        the dirty generation, so only the guard itself can see a switch that
        lands inside the tunnel send. Entry saw a plain session; the commit point
        must refuse the orchestrator it has become, or a plain remote copy is all
        that survives the archive."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)

        async def _send_while_mode_flips(instance_id, bundle):
            slot.mode = "orchestrator"
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_while_mode_flips
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_mode_unsupported"
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_a_reset_during_the_peer_round_trip_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        """A reset-conversation clears the resumable sid and touches NEITHER the
        message count NOR the dirty generation, so the two transcript anchors
        cannot see it. The session identity is the third anchor: a source whose
        live session is already gone must not be archived under a peer that
        resumed the pre-reset context."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)
        live = {"sid": "sid-before-reset"}
        state.sessions.resumable_sid = lambda key: live["sid"]  # type: ignore[method-assign]
        before = (len(slot.messages), slot._dirty_gen)

        async def _send_while_reset_lands(instance_id, bundle):
            live["sid"] = None  # what discard_conversation leaves behind
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_while_reset_lands
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        # The transcript anchors alone would have let this through.
        assert (len(slot.messages), slot._dirty_gen) == before
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False


class TestConsistencyAnchor:
    """The bundle is the consistency anchor: content the peer never received
    must not be archived out of reach, even when the racing turn has already
    finished by archive time."""

    @pytest.mark.asyncio
    async def test_a_turn_completed_within_the_awaits_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)

        async def _send_while_turn_completes(instance_id, bundle):
            # The racing turn runs to COMPLETION inside the tunnel send: no
            # running task remains, only a transcript longer than the bundle.
            slot.messages.append({"role": "user", "content": "raced question"})
            slot.messages.append({"role": "assistant", "content": "raced answer"})
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_while_turn_completes
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "turn_in_flight"
        # Source intact and unlocked; the raced content is still live-editable.
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False

    @pytest.mark.asyncio
    async def test_an_in_place_content_change_aborts_the_archive(self, tmp_path, monkeypatch):
        """A variant switch or edit changes content WITHOUT changing the
        message count; the dirty generation is the anchor that catches it."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)

        async def _send_while_variant_switches(instance_id, bundle):
            slot.messages[-1]["content"] = "the OTHER variant"
            slot._dirty_gen += 1  # every in-place mutation path bumps this
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_while_variant_switches
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None

    @pytest.mark.asyncio
    async def test_a_replacement_slot_under_the_same_name_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        """Identity is re-checked at the pre-pop boundary: archiving whatever
        holds the NAME now would close a conversation this migration never
        authorized."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)

        async def _send_and_replace(instance_id, bundle):
            state._slots.pop("s1", None)
            replacement = state.get_or_create_slot("s1")
            del replacement  # a different object now owns the name
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_and_replace
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
        # The replacement survives untouched under the name.
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None


class TestRoundSeventeen:
    """Peer error text crosses the relay trust boundary; accepted context is
    part of the conversation the bundle must carry or the archive must wait for."""

    @pytest.mark.asyncio
    async def test_peer_refusal_text_is_redacted_before_it_is_returned(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        mgr = _stub_transfer(state, monkeypatch)
        leaked = "peer refused: AKIAIOSFODNN7EXAMPLE was rejected"
        mgr.send_session_bundle = AsyncMock(
            return_value=(False, {"error": leaked, "code": "transfer_peer_refused"})
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 502
            body = await resp.json()
        assert "AKIAIOSFODNN7EXAMPLE" not in body["error"]
        assert body["error"].startswith("peer refused")
        assert body["code"] == "transfer_peer_refused"

    @pytest.mark.asyncio
    async def test_pending_context_refuses_at_entry(self, tmp_path):
        """append_pending_context touches neither the message count nor the
        dirty generation: the bundle would omit it and the anchor would not see it."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append_pending_context({"kind": "note", "text": "accepted for the next turn"})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_pending_context"
        assert "s1" in state._slots
        assert state._slots["s1"]._pending_context  # still owed to the next turn here

    @pytest.mark.asyncio
    async def test_deferred_notes_refuse_at_entry(self, tmp_path):
        """A held note is an acknowledged delivery whose durable copy replays
        only onto THIS key — which the archive would lock read-only."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._deferred_notes.append({"kind": "note", "text": "held"})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_pending_context"
        assert "s1" in state._slots

    @pytest.mark.asyncio
    async def test_context_accepted_during_the_peer_round_trip_aborts_the_archive(
        self, tmp_path, monkeypatch
    ):
        """The same guard runs at the commit point: context accepted inside the
        tunnel send is caught there, not archived out of reach."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        _seed_conversation(slot)
        mgr = _stub_transfer(state, monkeypatch)

        async def _send_while_context_lands(instance_id, bundle):
            slot.append_pending_context({"kind": "note", "text": "landed mid-transfer"})
            return (True, {"key": "peer-key-9"})

        mgr.send_session_bundle = _send_while_context_lands
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/migrate-remote", json={"instance_id": "crew-a"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "migrate_pending_context"
        assert "s1" in state._slots
        assert state._slots["s1"].migrated is None
        assert state._slots["s1"]._migrating is False
