"""Tests for chat_tags — assigning tags to a chat slot.

The slot tag endpoints: assignment and its validation, the add/remove delta,
status tags, the compare-and-set revision and persistence of the slot's list.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard.state import _ChatSlot

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret


# ── Slot tag assignment ──


class TestSlotTags:
    @pytest.mark.asyncio
    async def test_assign_filters_unknown_and_dedupes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            t1 = await (await client.post("/api/chat/tags", json={"name": "T1"})).json()
            t2 = await (await client.post("/api/chat/tags", json={"name": "T2"})).json()
            slot = _ChatSlot("s1")
            original_revision = slot.tags_revision
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [t1["id"], "ghost", t1["id"], t2["id"], 7]},
                )
            assert resp.status == 200
            data = await resp.json()
            assert data["tags"] == [t1["id"], t2["id"]]
            assert data["tags_revision"] == slot.tags_revision
            assert data["prior_tags_revision"] == original_revision
            assert slot.tags_revision != original_revision
            # Revisions are TOTALLY ORDERED, not merely opaque: a persisted
            # per-process epoch counter then a zero-padded sequence, so a client
            # can tell an older snapshot it never saw (a delayed HTTP fetch after
            # a newer WebSocket frame, or a slow pre-restart reply) from a newer
            # commit by string order alone. The successor must share the epoch
            # and sort strictly after its predecessor.
            import re as _re

            assert _re.fullmatch(r"\d{16}\.\d{20}-[0-9a-f]{8}", slot.tags_revision)
            assert _re.fullmatch(r"\d{16}\.\d{20}-[0-9a-f]{8}", original_revision)
            assert slot.tags_revision.split(".")[0] == original_revision.split(".")[0]
            assert slot.tags_revision > original_revision
            assert state.serialize_slot(slot)["tags_revision"] == slot.tags_revision
            assert slot.tags == [t1["id"], t2["id"]]

    @pytest.mark.asyncio
    async def test_assign_with_stale_base_revision_is_refused_without_writing(
        self, tmp_path, monkeypatch
    ):
        """Two clients: B commits t2 while A composes ``[t1]`` onto the earlier
        snapshot. A's write names that snapshot's revision as its base; the
        server compares under the tag lock, refuses without touching the slot,
        and returns the current list + revision so A can rebase its delta. The
        same list sent with the CURRENT revision (or no base at all) commits."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            t1 = await (await client.post("/api/chat/tags", json={"name": "T1"})).json()
            t2 = await (await client.post("/api/chat/tags", json={"name": "T2"})).json()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            stale_base = slot.tags_revision
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                # Client B commits t2 (unconditional legacy write).
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [t2["id"]]})
                assert resp.status == 200
                current = slot.tags_revision
                assert current != stale_base
                saves_before = save.call_count
                # Client A, composed onto the pre-B snapshot, tries to replace with [t1].
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [t1["id"]], "base_tags_revision": stale_base},
                )
                assert resp.status == 409
                body = await resp.json()
                assert body["code"] == "stale_base"
                assert body["base_tags_revision"] == stale_base
                assert body["tags_revision"] == current
                assert body["tags"] == [t2["id"]]
                assert slot.tags == [t2["id"]] and slot.tags_revision == current
                assert save.call_count == saves_before  # nothing written
                # A rebases its delta onto the returned list and retries.
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [t2["id"], t1["id"]], "base_tags_revision": current},
                )
                assert resp.status == 200
                assert slot.tags == [t2["id"], t1["id"]]
                assert (await resp.json())["prior_tags_revision"] == current
                # A non-string base is rejected up front.
                resp = await client.put(
                    "/api/chat/slots/s1/tags", json={"tags": [], "base_tags_revision": 5}
                )
                assert resp.status == 400
                assert (await resp.json())["code"] == "base_not_string"

    @pytest.mark.asyncio
    async def test_agent_mutation_is_visible_to_the_boards_cas(self, tmp_path, monkeypatch):
        """The agent's ``chat_tag`` directive and the human board edit the same
        slot. The board composes its PUT against the revision it last saw; an
        agent mutation that landed in between must rotate that revision, so the
        human's save is refused as ``stale_base`` and shown the agent's tags --
        not applied over them without a word."""
        from kiro_crew.dashboard.session_directive_apply import apply_session_directive

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            review = await (
                await client.post("/api/chat/tags", json={"name": "Review", "status": True})
            ).json()
            plain = await (await client.post("/api/chat/tags", json={"name": "Plain"})).json()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            human_base = slot.tags_revision

            async def _saved(state, slot, force=False, expected_history_key=None):
                return True

            with patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", _saved):
                result = await apply_session_directive(
                    state,
                    slot,
                    "dashboard:s1",
                    "chat_tag",
                    {"set_state": "Review"},
                    producer_is_user_facing=True,
                )
            assert not result.startswith("Error:"), result
            assert slot.tags == [review["id"]]
            agent_revision = slot.tags_revision
            assert agent_revision != human_base

            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [plain["id"]], "base_tags_revision": human_base},
                )
                assert resp.status == 409
                body = await resp.json()
                assert body["code"] == "stale_base"
                assert body["tags"] == [review["id"]] and body["tags_revision"] == agent_revision
                assert slot.tags == [review["id"]]  # the agent's change stands
                assert save.call_count == 0
                # Rebased onto what the agent wrote, the human's edit commits.
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={
                        "tags": [review["id"], plain["id"]],
                        "base_tags_revision": agent_revision,
                    },
                )
                assert resp.status == 200
                assert slot.tags == [review["id"], plain["id"]]

    @pytest.mark.asyncio
    async def test_assign_refusal_restores_tags_and_revision(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "T1"})).json()
            slot = _ChatSlot("s1")
            slot.tags = ["existing"]
            original_revision = slot.tags_revision
            state._slots["s1"] = slot

            async def _refuse(*_args, **_kwargs):
                return False

            state.push_slots_update = MagicMock()
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", _refuse):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [tag["id"]]})

            assert resp.status == 409
            assert slot.tags == ["existing"]
            body = await resp.json()
            assert body["code"] == "session_gone"
            # The provisional revision may have leaked via a concurrent slots
            # broadcast while the save awaited; the rejection must name it so
            # clients can classify that frame as stale, not as a newer writer.
            assert body["rejected_tags_revision"]
            assert body["rejected_tags_revision"] != original_revision
            # The rollback mints a FRESH revision rather than restoring the
            # prior one: a client that adopted the leaked revision already
            # treats the prior one as a known predecessor and would keep the
            # rejected tags. The fresh revision is broadcast so it reconverges.
            assert slot.tags_revision != original_revision
            assert slot.tags_revision != body["rejected_tags_revision"]
            assert body["tags_revision"] == slot.tags_revision
            # The rolled-back list rides along so a client can seed its accepted
            # snapshot from the server's post-rollback state before retrying.
            assert body["tags"] == ["existing"]
            state.push_slots_update.assert_called()

    def test_revision_epoch_persists_and_orders_across_restarts(self, tmp_path, monkeypatch):
        """A restarted process claims a higher epoch from the persisted counter,
        so every revision it mints sorts after every revision of the previous
        process — however far the old process's sequence had advanced."""
        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Process 1: fresh data home, seeds the counter and mints many revisions.
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_SEQ", 0)
        old_revisions = [state_module.mint_tags_revision() for _ in range(50)]
        assert old_revisions == sorted(old_revisions)
        epoch_file = tmp_path / state_module._TAGS_REVISION_EPOCH_FILE
        first_epoch = int(epoch_file.read_text().strip())
        assert first_epoch > 0
        # Process 2 ("restart"): re-claims (>= counter + 1 and >= clock) and
        # starts its sequence over.
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_SEQ", 0)
        new_revision = state_module.mint_tags_revision()
        assert int(epoch_file.read_text().strip()) > first_epoch
        assert new_revision.split(".")[1].startswith("0" * 19 + "1")
        assert all(new_revision > old for old in old_revisions)
        # A backward clock step cannot regress the claim below the counter.
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        # flake-ok: pre-existing patch of the stdlib time.time_ns the state module reads, undone by monkeypatch at teardown; moved verbatim, tracked for a de-flake pass
        monkeypatch.setattr(state_module.time, "time_ns", lambda: 1_000_000)
        assert state_module.ensure_tags_revision_epoch() == int(epoch_file.read_text().strip())
        assert state_module.ensure_tags_revision_epoch() > int(new_revision.split(".")[0])

    def test_unpersisted_epoch_mints_opaque_revisions(self, tmp_path, monkeypatch):
        """When the epoch counter cannot be persisted, no ordering may be asserted
        (the next restart cannot know about it, and a backward clock step could
        then yield a LOWER orderable epoch that clients would reject). The process
        mints opaque revisions instead, which clients treat with equality and
        lineage only; a later writable restart persists a real epoch again and
        orderable minting resumes."""
        import re as _re

        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        epoch_file = tmp_path / state_module._TAGS_REVISION_EPOCH_FILE
        epoch_file.write_text("41\n")
        real_atomic_write = state_module.atomic_write

        def _unwritable(*_args, **_kwargs):
            raise OSError("read-only data home")

        monkeypatch.setattr(state_module, "atomic_write", _unwritable)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_SEQ", 0)
        assert state_module.ensure_tags_revision_epoch() is None
        opaque = [state_module.mint_tags_revision() for _ in range(3)]
        assert all(_re.fullmatch(r"[0-9a-f]{32}", r) for r in opaque)
        assert len(set(opaque)) == 3
        assert epoch_file.read_text().strip() == "41"  # nothing persisted
        # The failed claim is remembered: no disk retry on every mint.
        assert state_module._TAGS_REVISION_EPOCH == state_module._TAGS_REVISION_EPOCH_UNPERSISTED

        # Home becomes writable again on a later restart: orderable minting resumes.
        monkeypatch.setattr(state_module, "atomic_write", real_atomic_write)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_SEQ", 0)
        recovered = state_module.mint_tags_revision()
        assert _re.fullmatch(r"\d{16}\.\d{20}-[0-9a-f]{8}", recovered)
        assert int(epoch_file.read_text().strip()) == int(recovered.split(".")[0]) > 41

    def test_epoch_claim_is_durable_before_it_is_used(self, tmp_path, monkeypatch):
        """The anti-regression guarantee rests on the persisted counter surviving
        a crash. The claim must fsync the file's data AND sync the parent
        directory (the rename's entry) before any revision is minted from it;
        otherwise a power loss inside the flush window followed by a backward
        clock step re-claims an epoch that connected clients already hold."""
        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        calls: list[tuple[str, object]] = []
        real_atomic_write = state_module.atomic_write

        def _recording_write(path, data, **kwargs):
            calls.append(("write", kwargs.get("fsync", False)))
            return real_atomic_write(path, data, **kwargs)

        def _recording_fsync_dir(path, **_kwargs):
            calls.append(("sync_dir", Path(path)))

        monkeypatch.setattr(state_module, "atomic_write", _recording_write)
        monkeypatch.setattr(state_module, "fsync_dir", _recording_fsync_dir)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_SEQ", 0)
        claimed = state_module.ensure_tags_revision_epoch()
        assert claimed is not None and claimed > 0
        assert calls == [("write", True), ("sync_dir", tmp_path)]

        # A directory sync that reports the device refused the write is a failed
        # claim: the counter is not durable, so ordering must not be asserted.
        def _failing_fsync_dir(path, **_kwargs):
            raise OSError(5, "EIO")

        monkeypatch.setattr(state_module, "fsync_dir", _failing_fsync_dir)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        assert state_module.ensure_tags_revision_epoch() is None

    def test_malformed_epoch_file_mints_opaque_revisions(self, tmp_path, monkeypatch):
        """A counter that cannot be parsed is not "no counter": the real value may
        exceed anything the clock now yields, so re-seeding from the clock could
        persist a LOWER epoch than clients already hold (a backward clock step
        after the corruption). The process must mint opaque revisions and leave
        the file alone rather than assert an order it cannot prove."""
        import re as _re

        from kiro_crew.dashboard import state as state_module

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        epoch_file = tmp_path / state_module._TAGS_REVISION_EPOCH_FILE
        epoch_file.write_text("garbage\n")
        # Backward clock: far below any epoch a healthy process would have claimed.
        # flake-ok: pre-existing patch of the stdlib time.time_ns the state module reads, undone by monkeypatch at teardown; moved verbatim, tracked for a de-flake pass
        monkeypatch.setattr(state_module.time, "time_ns", lambda: 1_000_000)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_EPOCH", None)
        monkeypatch.setattr(state_module, "_TAGS_REVISION_SEQ", 0)
        assert state_module.ensure_tags_revision_epoch() is None
        assert _re.fullmatch(r"[0-9a-f]{32}", state_module.mint_tags_revision())
        assert epoch_file.read_text() == "garbage\n"  # never overwritten with a low epoch

    @pytest.mark.asyncio
    async def test_assign_slot_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/chat/slots/ghost/tags", json={"tags": []})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_assign_invalid_json_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/s1/tags",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_assign_non_array_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._slots["s1"] = _ChatSlot("s1")
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/chat/slots/s1/tags", json={"tags": "not-a-list"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_assign_app_token_denied_on_foreign_slot(self, tmp_path, monkeypatch):
        """An app caller gets the indistinguishable 404 and writes nothing.

        Tagging is a write to a session's own state, the same class as filing
        (``api_chat_slot_folder``), and the ``chat_tag_assign`` MCP tool reaches
        this route on behalf of app agents whose visibility is only scoped
        client-side — so the boundary must hold here. Both an UNSCOPED slot and
        another app's slot answer ``slot_not_found``: a distinct code per reason
        would make the route an existence oracle.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        unscoped = _ChatSlot("s1")
        foreign = _ChatSlot("s2")
        foreign._app = "other-app"
        state._slots["s1"] = unscoped
        state._slots["s2"] = foreign
        app = _make_tags_app(state)

        from aiohttp import web as _web

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "attacker-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            # Seeded directly: an app caller may not POST a tag (see
            # test_create_refuses_an_app_caller), and this test is about the PUT.
            tag = {"id": "t1", "name": "T1", "color": "#000000", "order": 0, "status": False}
            state._tags.append(tag)
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                for key in ("s1", "s2"):
                    resp = await client.put(
                        f"/api/chat/slots/{key}/tags", json={"tags": [tag["id"]]}
                    )
                    assert resp.status == 404
                    assert (await resp.json())["code"] == "slot_not_found"
            save.assert_not_called()
        assert unscoped.tags == [] and foreign.tags == []

    @pytest.mark.asyncio
    async def test_assign_app_token_denied_when_slot_is_linked_to_a_foreign_transcript(
        self, tmp_path, monkeypatch
    ):
        """``_app`` alone is not ownership: the write lands on the LINKED transcript.

        An app-owned slot whose ``linked_session_key`` names another owner's
        session would pass an ``_app`` check and persist the app's tags into a
        conversation it does not own. Both identities must resolve to the app.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        mine = _ChatSlot("s1")
        mine._app = "my-app"
        theirs = _ChatSlot("s2")
        theirs._app = "other-app"
        theirs.linked_session_key = "taskrunner:t1:chat:abc"
        mine.linked_session_key = "taskrunner:t1:chat:abc"  # rebound onto the other app's session
        state._slots["s1"] = mine
        state._slots["s2"] = theirs
        state._tags.append(
            {"id": "t1", "name": "T1", "color": "#000000", "order": 0, "status": False}
        )
        app = _make_tags_app(state)

        from aiohttp import web as _web

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": ["t1"]})
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
            save.assert_not_called()
        assert mine.tags == []

    def test_app_owns_transcript_is_unanimous_and_fails_closed(self):
        """Own file and own linked session pass; shared, foreign, unclaimed or empty keys do not."""
        from kiro_crew.dashboard.token_auth import app_owns_transcript

        mine = _ChatSlot("s1")
        mine._app = "my-app"
        theirs = _ChatSlot("s2")
        theirs._app = "other-app"
        slots = {"s1": mine, "s2": theirs}
        assert app_owns_transcript(slots, "", "anything") is True  # the person
        assert app_owns_transcript(slots, "my-app", "dashboard:s1") is True
        assert app_owns_transcript(slots, "my-app", "dashboard:s2") is False
        mine.linked_session_key = "taskrunner:t9:chat:xyz"
        assert app_owns_transcript(slots, "my-app", "taskrunner:t9:chat:xyz") is True
        theirs.linked_session_key = "taskrunner:t9:chat:xyz"  # now shared with another owner
        assert app_owns_transcript(slots, "my-app", "taskrunner:t9:chat:xyz") is False
        # An unbound channel-origin slot's transcript is claimed by no slot.
        assert app_owns_transcript(slots, "my-app", "slack:C1.unbound") is False
        # A channel transcript is the person's even when only app slots are bound
        # to it — the app's own slots cannot vouch for it.
        mine.linked_session_key = "slack:C7.700"
        assert app_owns_transcript(slots, "my-app", "slack:C7.700") is False
        assert app_owns_transcript(slots, "my-app", "") is False

    @pytest.mark.asyncio
    async def test_assign_app_token_denied_when_a_foreign_slot_binds_mid_request(
        self, tmp_path, monkeypatch
    ):
        """Ownership is re-asked under the write lock, not only before the awaits.

        The pre-await check passes (the app's slot is the only claimant); a
        foreign slot then binds to the same transcript while the body is being
        read. The under-lock re-check must see the new claimant and refuse
        with nothing saved — otherwise the earlier answer is stale by the time
        the write lands.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        mine = _ChatSlot("s1")
        mine._app = "my-app"
        mine.linked_session_key = "taskrunner:t1:chat:abc"
        theirs = _ChatSlot("s2")
        theirs._app = "other-app"
        state._slots["s1"] = mine
        state._slots["s2"] = theirs
        state._tags.append(
            {"id": "t1", "name": "T1", "color": "#000000", "order": 0, "status": False}
        )
        app = _make_tags_app(state)

        from aiohttp import web as _web

        from kiro_crew.dashboard import chat_tags as _ct

        real_read = _ct.read_bounded_json

        async def _bind_then_read(request):
            theirs.linked_session_key = "taskrunner:t1:chat:abc"  # lands during the body await
            return await real_read(request)

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            with (
                patch(
                    "kiro_crew.dashboard.chat_tags.read_bounded_json", side_effect=_bind_then_read
                ),
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save,
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": ["t1"]})
            assert resp.status == 409
            assert (await resp.json())["code"] == "session_gone"
            save.assert_not_called()
        assert mine.tags == []

    @pytest.mark.asyncio
    async def test_assign_refuses_a_caller_whose_slot_is_gone(self, tmp_path, monkeypatch):
        """The closed-tab race on the per-slot write, same guard as the vocabulary writes.

        A ``dashboard:`` key naming a popped slot derives app "" and would read
        as the person, passing both ownership checks. Refused before either.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        target = _ChatSlot("s1")
        state._slots["s1"] = target
        state._tags.append(
            {"id": "t1", "name": "T1", "color": "#000000", "order": 0, "status": False}
        )
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": ["t1"]},
                    headers={"X-Session-Key": "dashboard:gone-slot"},
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "caller_unattributable"
            save.assert_not_called()
        assert target.tags == []

    @pytest.mark.asyncio
    async def test_assign_app_token_allowed_on_its_own_linked_transcript(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        own = _ChatSlot("s1")
        own._app = "my-app"
        own.linked_session_key = "taskrunner:t9:chat:xyz"
        state._slots["s1"] = own
        state._tags.append(
            {"id": "t1", "name": "T1", "color": "#000000", "order": 0, "status": False}
        )
        app = _make_tags_app(state)

        from aiohttp import web as _web

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": ["t1"]})
            assert resp.status == 200
        assert own.tags == ["t1"]

    @pytest.mark.asyncio
    async def test_assign_app_token_allowed_on_own_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        own = _ChatSlot("s1")
        own._app = "my-app"
        state._slots["s1"] = own
        app = _make_tags_app(state)

        from aiohttp import web as _web

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            tag = {"id": "t1", "name": "T1", "color": "#000000", "order": 0, "status": False}
            state._tags.append(tag)
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [tag["id"]]})
            assert resp.status == 200
        assert own.tags == [tag["id"]]

    # ── Agent tag-grants policy on PUT /tags ──
    #
    # ``chat_tag`` ``set_state`` and the board drop route refuse an agent that
    # sets or strips a tag the person reserved; ``chat_tag_assign`` reaches this
    # route, so the same policy applies to the add/remove it implies. An
    # INTERNAL (MCP) caller presents ``X-Internal-Secret`` (validated upstream
    # by the token-auth middleware); the browser does not and is never gated.
    _INTERNAL = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_add_a_person_reserved_tag(self, tmp_path, monkeypatch):
        """An agent adding a ``none``-policy (person-reserved) tag is refused;
        nothing is written."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Reserved"})).json()
            chat_tag_grants.mint_grant(tag["id"], policy="none", status=False)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [tag["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 403
            body = await resp.json()
            assert body["code"] == "tag_policy_denied"
            assert body["error"] == f"tag_policy_denied:{tag['id']}"
            assert slot.tags == []  # unapplied
            assert save.call_count == 0  # never persisted

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_strip_an_add_only_tag(self, tmp_path, monkeypatch):
        """Removing needs ``add-remove``: an agent stripping an ``add-only`` tag
        the person placed is refused, leaving the tag on the slot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Sticky"})).json()
            chat_tag_grants.mint_grant(tag["id"], policy="add-only", status=False)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags", json={"tags": []}, headers=self._INTERNAL
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "tag_policy_denied"
            assert slot.tags == [tag["id"]]  # not stripped
            assert save.call_count == 0

    @pytest.mark.asyncio
    async def test_internal_caller_can_change_an_add_remove_tag(self, tmp_path, monkeypatch):
        """The gate is a policy check, not a blanket agent ban: an ``add-remove``
        tag the person opened to the agent is still assignable."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Open"})).json()
            chat_tag_grants.mint_grant(tag["id"], policy="add-remove", status=False)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [tag["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 200
            assert slot.tags == [tag["id"]]

    @pytest.mark.asyncio
    async def test_browser_is_never_gated_by_the_grants_policy(self, tmp_path, monkeypatch):
        """The person (no ``X-Internal-Secret``) may set a person-reserved tag;
        the whole policy constrains agents, not the owner."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = await (await client.post("/api/chat/tags", json={"name": "Reserved"})).json()
            chat_tag_grants.mint_grant(tag["id"], policy="none", status=False)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags", json={"tags": [tag["id"]]}
                )  # no internal-secret header -> browser
            assert resp.status == 200
            assert slot.tags == [tag["id"]]

    @pytest.mark.asyncio
    async def test_internal_caller_untouched_person_tag_is_not_regated(self, tmp_path, monkeypatch):
        """Only the DIFF is gated: an agent write that leaves a person-reserved
        tag already on the slot in place (adding an open tag beside it) is
        allowed — the reserved tag is neither added nor removed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            reserved = await (await client.post("/api/chat/tags", json={"name": "Reserved"})).json()
            open_tag = await (await client.post("/api/chat/tags", json={"name": "Open"})).json()
            chat_tag_grants.mint_grant(reserved["id"], policy="none", status=False)
            chat_tag_grants.mint_grant(open_tag["id"], policy="add-remove", status=False)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            slot.tags = [reserved["id"]]
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [reserved["id"], open_tag["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 200
            assert slot.tags == [reserved["id"], open_tag["id"]]

    @pytest.mark.asyncio
    async def test_app_token_cannot_strip_a_person_reserved_tag(self, tmp_path, monkeypatch):
        """Deny-by-default on identity: an app token (no internal secret, but a
        resolved app claim) is a non-person principal and is gated too, so it
        cannot strip a row-backed person-reserved tag."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from aiohttp import web as _web

        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        @_web.middleware
        async def _as_app(request, handler):
            request["app"] = "my-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            tag = {
                "id": "reserved1",
                "name": "Reserved",
                "color": "#000000",
                "order": 0,
                "status": True,
            }
            state._tags.append(tag)
            chat_tag_grants.mint_grant(tag["id"], policy="none", status=True)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            slot._app = "my-app"
            slot.tags = [tag["id"]]
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": []})
            assert resp.status == 403
            assert (await resp.json())["code"] in (
                "tag_policy_denied",
                "status_identity_unprotected",
            )
            assert slot.tags == [tag["id"]]
            assert save.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_internal_caller_cannot_stack_two_status_tags(self, tmp_path, monkeypatch):
        """Workflow states are mutually exclusive. An agent replace whose result
        carries two row-backed status tags is refused — the agent must use
        ``chat_tag`` ``set_state`` (which strips peers). Swapping one status for
        another (its peer removed in the same diff) is allowed."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            review = await (await client.post("/api/chat/tags", json={"name": "Review"})).json()
            done = await (await client.post("/api/chat/tags", json={"name": "Done"})).json()
            chat_tag_grants.mint_grant(review["id"], policy="add-remove", status=True)
            chat_tag_grants.mint_grant(done["id"], policy="add-remove", status=True)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            slot.tags = [review["id"]]
            state._slots["s1"] = slot
            # Stacking Done beside Review -> two status tags -> refused.
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [review["id"], done["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "status_tag_requires_set_state"
            assert slot.tags == [review["id"]]
            assert save.call_count == 0
            # Swapping Review -> Done (Review removed in the same diff) is fine.
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [done["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 200
            assert slot.tags == [done["id"]]

    @pytest.mark.asyncio
    async def test_internal_caller_may_assign_a_rowless_ordinary_label(self, tmp_path, monkeypatch):
        """A tag with no protected grant row is an ordinary label, not a
        reservation: a non-person caller may still apply it. This is what keeps
        app self-tagging working — the gate bites only tags the owner minted a
        row for."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            tag = {"id": "label1", "name": "Label", "color": "#000000", "order": 0, "status": False}
            state._tags.append(tag)  # rowless: resolves to ("none", False)
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [tag["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 200
            assert slot.tags == [tag["id"]]

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_add_a_rowless_status_tag(self, tmp_path, monkeypatch):
        """A rowless tag whose vocabulary ``status`` bit is true has an identity
        the store cannot vouch for. Adding it would stack a workflow state the
        peer-strip can never remove (``set_state`` would wedge), so it is refused
        ``status_identity_unprotected`` — a forged status cannot slip the gate by
        being rowless."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            # Vocabulary says status, but no grant row (an internal chat_tag_create
            # leaves it rowless).
            tag = {
                "id": "forged1",
                "name": "Forged",
                "color": "#000000",
                "order": 0,
                "status": True,
            }
            state._tags.append(tag)
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [tag["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "status_identity_unprotected"
            assert slot.tags == []
            assert save.call_count == 0

    @pytest.mark.asyncio
    async def test_pre_existing_two_status_does_not_block_an_unrelated_change(
        self, tmp_path, monkeypatch
    ):
        """The person (never gated) can leave a session carrying two status tags.
        A later agent change that touches neither — a pure remove of its own
        ordinary label — must not be blamed for that pre-existing state: the
        exclusivity refusal fires only when THIS diff adds a status tag."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tag_grants

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        async with TestClient(TestServer(app)) as client:
            review = await (await client.post("/api/chat/tags", json={"name": "Review"})).json()
            done = await (await client.post("/api/chat/tags", json={"name": "Done"})).json()
            label = {"id": "lbl", "name": "Lbl", "color": "#000000", "order": 0, "status": False}
            state._tags.append(label)
            chat_tag_grants.mint_grant(review["id"], policy="add-remove", status=True)
            chat_tag_grants.mint_grant(done["id"], policy="add-remove", status=True)
            chat_tag_grants.refresh_cache()
            # Pre-existing two status tags (as if the person placed both) plus an
            # ordinary label.
            slot = _ChatSlot("s1")
            slot.tags = [review["id"], done["id"], label["id"]]
            state._slots["s1"] = slot
            # Agent removes only its own ordinary label; the two status tags are
            # untouched -> allowed, not blamed for the pre-existing stack.
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [review["id"], done["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 200
            assert slot.tags == [review["id"], done["id"]]

    @pytest.mark.asyncio
    async def test_internal_caller_fails_closed_when_grants_store_degraded(
        self, tmp_path, monkeypatch
    ):
        """When the grants store cannot be verified (unreadable / gone /
        unrepaired quarantine), a reservation cannot be checked, so a non-person
        write is refused ``tag_grants_unavailable`` rather than letting a
        possibly-reserved tag through as rowless. The health and the rows come
        from ONE atomic snapshot, so a refresh cannot flip health between the
        check and a row lookup."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tags as ct
        from kiro_crew.dashboard.chat_tag_grants import GrantsSnapshot

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        # Force a write-blocked snapshot regardless of the on-disk store.
        monkeypatch.setattr(
            ct,
            "capture_grants_snapshot",
            lambda: GrantsSnapshot("unreadable", "unreadable", {}),
        )
        async with TestClient(TestServer(app)) as client:
            tag = {"id": "lbl2", "name": "Lbl2", "color": "#000000", "order": 0, "status": False}
            state._tags.append(tag)
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [tag["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "tag_grants_unavailable"
            assert slot.tags == []
            assert save.call_count == 0

    @pytest.mark.asyncio
    async def test_internal_caller_after_repaired_quarantine(self, tmp_path, monkeypatch):
        """A boot quarantine that has reseeded and VERIFIED (write_blocked clear)
        still has grants_reduced set, because the operator's custom reservations
        were discarded. A tag that is now rowless might be a lost reservation, so
        it is failed closed ``tag_grants_unavailable`` — but a ROW-BACKED tag (a
        reseeded default state) resolves normally and stays assignable."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_tags as ct
        from kiro_crew.dashboard.chat_tag_grants import GrantsSnapshot

        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        # Reseeded default state is row-backed add-remove; the operator's former
        # custom label is gone from the rows (quarantine discarded it).
        reseeded_rows = {"review": ("add-remove", True)}
        monkeypatch.setattr(
            ct,
            "capture_grants_snapshot",
            # write_blocked clear (reseed verified), grants_reduced still set.
            lambda: GrantsSnapshot(None, "quarantined", reseeded_rows),
        )
        async with TestClient(TestServer(app)) as client:
            review = {
                "id": "review",
                "name": "Review",
                "color": "#000000",
                "order": 0,
                "status": True,
            }
            lost = {
                "id": "lostlabel",
                "name": "Lost",
                "color": "#000000",
                "order": 1,
                "status": False,
            }
            state._tags.extend([review, lost])
            # A formerly-reserved (now rowless) label is failed closed.
            slot = _ChatSlot("s1")
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [lost["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "tag_grants_unavailable"
            assert slot.tags == []
            assert save.call_count == 0
            # A reseeded default state (row-backed add-remove) still assigns.
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags",
                    json={"tags": [review["id"]]},
                    headers=self._INTERNAL,
                )
            assert resp.status == 200
            assert slot.tags == [review["id"]]

    @pytest.mark.asyncio
    async def test_internal_caller_cannot_strip_an_unresolvable_tag_when_vocab_nonauthoritative(
        self, tmp_path, monkeypatch
    ):
        """When ``tags.json`` was unreadable at boot the vocabulary is empty and
        NON-authoritative, yet protected rows for reserved tags persist. A tag
        on the slot is then absent from ``by_id``, so a diff built only from the
        vocabulary would silently omit its removal and persist it with no grants
        check. The route fails closed ``tag_grants_unavailable`` for a removal it
        cannot resolve while the vocabulary is non-authoritative."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        # Simulate an unreadable tags.json: empty, non-authoritative vocabulary.
        state._tags = []
        state._tags_authoritative = False
        async with TestClient(TestServer(app)) as client:
            slot = _ChatSlot("s1")
            slot.tags = ["review"]  # a reserved status id not present in the (empty) vocab
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop") as save:
                resp = await client.put(
                    "/api/chat/slots/s1/tags", json={"tags": []}, headers=self._INTERNAL
                )
            assert resp.status == 403
            assert (await resp.json())["code"] == "tag_grants_unavailable"
            assert slot.tags == ["review"]  # removal not persisted
            assert save.call_count == 0

    @pytest.mark.asyncio
    async def test_authoritative_vocab_allows_dropping_a_dangling_id(self, tmp_path, monkeypatch):
        """With an AUTHORITATIVE vocabulary, a slot id absent from it is a
        dangling reference to a deleted tag — benign for an agent to drop. The
        non-authoritative fail-closed does not fire here."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        state._tags = []  # authoritative-and-empty: the id really was deleted
        state._tags_authoritative = True
        async with TestClient(TestServer(app)) as client:
            slot = _ChatSlot("s1")
            slot.tags = ["ghost"]
            state._slots["s1"] = slot
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put(
                    "/api/chat/slots/s1/tags", json={"tags": []}, headers=self._INTERNAL
                )
            assert resp.status == 200
            assert slot.tags == []

    @pytest.mark.asyncio
    async def test_non_person_signal_enumeration_each_leg_gates(self, tmp_path, monkeypatch):
        """Regression guard on the gate's non-person enumeration AS IT STANDS.
        Person-identity is the absence of three signals — internal-secret
        transport, a resolved app claim, a ``member:`` principal — and this test
        pins that each of those three independently causes gating while the
        person (presenting none) is not gated, on a row-backed person-reserved
        tag. It does NOT prove a hypothetical fourth principal class would fail:
        the enumeration is the accepted contract (keying on the mint path's
        positive owner predicate was rejected because it gates the person on a
        no-owner install), so a new auth representation must be added to this
        enumeration AND to this test together. The sibling fences
        (``member_slot_write_refused``, the app-isolation check) key on the same
        three representations."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from aiohttp import web as _web

        from kiro_crew.dashboard import chat_tag_grants
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

        async def _put(request_markers, slot_setup=None):
            state = _make_state(tmp_path)
            tag = {"id": "resv", "name": "Resv", "color": "#000000", "order": 0, "status": False}
            state._tags = [tag]
            chat_tag_grants.mint_grant(tag["id"], policy="none", status=False)
            chat_tag_grants.refresh_cache()
            slot = _ChatSlot("s1")
            if slot_setup is not None:
                slot_setup(state, slot)
            state._slots["s1"] = slot
            app = _make_tags_app(state)

            @_web.middleware
            async def _mark(request, handler):
                for k, v in request_markers.get("req", {}).items():
                    request[k] = v
                return await handler(request)

            app.middlewares.append(_mark)
            async with TestClient(TestServer(app)) as client:
                with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                    resp = await client.put(
                        "/api/chat/slots/s1/tags",
                        json={"tags": [tag["id"]]},
                        headers=request_markers.get("headers", {}),
                    )
                body = await resp.json()
                return resp.status, slot.tags, body.get("code")

        # Person: no signal -> NOT gated (the reserved tag is applied).
        status, tags, _ = await _put({})
        assert status == 200 and tags == ["resv"]
        # Internal-secret transport signal -> gated by the grants gate itself.
        status, _, code = await _put({"headers": self._INTERNAL})
        assert status == 403 and code == "tag_policy_denied"
        # App-claim signal -> the app LEGITIMATELY owns the slot (``_app``
        # matches, so the app-isolation fence passes and ``app_owns_transcript``
        # is satisfied for the slot's own transcript), so the request reaches
        # the GRANTS GATE, which refuses it. Asserting the gate's own
        # ``tag_policy_denied`` (not merely ``!= 200``) means dropping
        # ``request_app`` from ``is_internal_caller`` would turn this 403 into a
        # 200 and fail the pin -- the sibling fence cannot stand in for the gate.

        def _own_app(state, slot):
            slot._app = "an-app"

        status, _, code = await _put({"req": {"app": "an-app"}}, slot_setup=_own_app)
        assert status == 403 and code == "tag_policy_denied"
        # Member-principal signal -> the member LEGITIMATELY owns the slot (its
        # own session key is the slot's), so the member-ownership fence passes
        # and the request reaches the GRANTS GATE. Same reasoning: asserting
        # ``tag_policy_denied`` means dropping the ``member:`` arm from
        # ``is_internal_caller`` would surface a 200 and fail the pin.
        member_key = effective_session_key(_ChatSlot("s1"))

        def _own_member(state, slot):
            slot._created_by = ""

        status, _, code = await _put(
            {
                "req": {MEMBER_CHAT_PRINCIPAL_KEY: "member:store-x"},
                "headers": {"X-Session-Key": member_key},
            },
            slot_setup=_own_member,
        )
        assert status == 403 and code == "tag_policy_denied"

    @pytest.mark.asyncio
    async def test_denial_audit_attributes_the_authenticated_principal(self, tmp_path, monkeypatch):
        """The grants-policy denial audit must record the authenticated app or
        member principal, not the transport origin. ``request_origin`` yields
        ``dashboard`` for an app-token or member caller on this route, so a bare
        ``caller=origin_caller`` would file every denial under ``dashboard`` and
        corrupt the append-only SEL attribution. Pin that the app leg's denial
        records the app id."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from aiohttp import web as _web

        from kiro_crew.dashboard import chat_tag_grants

        captured: list[dict] = []

        class _CapturingSel:
            def log_api_access(self, **kwargs):
                captured.append(kwargs)

        monkeypatch.setattr("kiro_crew.dashboard.chat_tags.sel", lambda: _CapturingSel())

        state = _make_state(tmp_path)
        tag = {"id": "resv", "name": "Resv", "color": "#000000", "order": 0, "status": False}
        state._tags = [tag]
        chat_tag_grants.mint_grant(tag["id"], policy="none", status=False)
        chat_tag_grants.refresh_cache()
        slot = _ChatSlot("s1")
        slot._app = "an-app"
        state._slots["s1"] = slot
        app = _make_tags_app(state)

        @_web.middleware
        async def _mark(request, handler):
            request["app"] = "an-app"
            return await handler(request)

        app.middlewares.append(_mark)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [tag["id"]]})
        assert resp.status == 403
        denials = [c for c in captured if c.get("outcome") == "denied"]
        assert denials, "expected a denial audit"
        # The app principal, not the transport origin ``dashboard``.
        assert denials[-1]["caller"] == "an-app"
