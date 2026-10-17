"""`PATCH /api/chat/slots/{slot}/mutes-opened` is a dashboard-user-only write.

The "mute sessions it opens" flag is the user's own decision: ``session_create``'s
contract forbids an agent opening already-silenced sessions, so no agent or MCP
route may set it. The slot-ownership fences stop a FOREIGN caller, but an app or
internal-agent caller writing to a slot it owns would still pass them -- a shipped
built-in app holding the ``/api/chat/slots/*`` grant could otherwise flip the flag
with no user action. The handler therefore requires positive dashboard-human
provenance (``is_dashboard_user`` and not ``internal_auth``) before any mutation.

It persists the new value BEFORE it publishes it, under the transcript lock. The
live ``slot.mutes_opened`` is left at its committed value across the whole save;
the NEW value is persisted as a STAGED override that both save paths honor -- the
full line AND the empty-window merge of a message-less newborn (the round-1 F1
gap, where the merge ignored the staged value). The live flag is flipped only
AFTER the write commits, inside the transcript lock, via an
``after_commit_under_lock`` hook -- so a concurrent dirty-slot flush, which needs
the same lock to serialize the slot, cannot interleave and resurrect the prior
value (the round-1 F2 race). Because the live flag never carries the provisional
value until the write commits, a concurrent ``/api/chat/slots`` GET or a
worker-completion broadcast during the save window reads the committed prior
value -- so a failed or timed-out save changes nothing and publishes nothing, and
no corrective frame is owed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_folders import api_chat_slot_mutes_opened
from kiro_crew.dashboard.state import _ChatSlot


def _make_app(
    state: MagicMock, *, declared_app: str = "", internal_auth: bool = False
) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_identity(request: web.Request, handler):
        # Mirror token_auth_middleware: an app token publishes its name and is
        # not a dashboard user; the internal-secret (MCP/cron) transport carries
        # no app claim but sets internal_auth. A real dashboard user has an empty
        # app and is_dashboard_user True.
        request["app"] = declared_app
        request["user"] = "local-app"
        request["internal_auth"] = internal_auth
        request["is_dashboard_user"] = declared_app == "" and not internal_auth
        return await handler(request)

    app.middlewares.append(_publish_identity)
    app.router.add_patch("/api/chat/slots/{slot}/mutes-opened", api_chat_slot_mutes_opened)
    return app


def _state(slot: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state._slots = {slot.key: slot}
    state.push_slot_patch = MagicMock()
    return state


def _owned_slot(key: str, app: str = "") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


# All four ownership fences pass for a caller writing to a slot it owns, so these
# tests isolate the dashboard-user provenance gate on top of them.
def _pass_all_fences():
    return (
        patch("kiro_crew.dashboard.chat_folders.refuse_unattributable_caller", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.member_slot_write_refused", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.deny_app_slot_access", return_value=None),
        patch("kiro_crew.dashboard.chat_folders.app_owns_transcript", return_value=True),
        patch("kiro_crew.dashboard.chat_folders._effective_request_app", return_value=""),
    )


class TestMuteIsDashboardUserOnly:
    @pytest.mark.asyncio
    async def test_app_caller_owning_its_slot_is_refused(self) -> None:
        slot = _owned_slot("chat-1-100", app="auto-improvement")
        state = _state(slot)
        f1, f2, f3, f4, f5 = _pass_all_fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch(
                "kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock(return_value=True)
            ) as save,
        ):
            app = _make_app(state, declared_app="auto-improvement")
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "mutes_opened_user_only"
        assert slot.mutes_opened is False  # never mutated
        save.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_agent_caller_is_refused(self) -> None:
        slot = _owned_slot("chat-1-100")
        state = _state(slot)
        f1, f2, f3, f4, f5 = _pass_all_fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch(
                "kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock(return_value=True)
            ) as save,
        ):
            app = _make_app(state, internal_auth=True)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
                body = await resp.json()
        assert resp.status == 403
        assert body["code"] == "mutes_opened_user_only"
        assert slot.mutes_opened is False
        save.assert_not_called()

    @pytest.mark.asyncio
    async def test_dashboard_user_is_allowed(self) -> None:
        slot = _owned_slot("chat-1-100")
        state = _state(slot)
        f1, f2, f3, f4, f5 = _pass_all_fences()

        async def _commit(*args, **kwargs):
            # A committed write invokes the after-commit hook under the lock,
            # which flips the live flag (the handler wires it to do so).
            kwargs["after_commit_under_lock"]()
            return True

        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _commit),
        ):
            app = _make_app(state)  # dashboard user: empty app, no internal_auth
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
                body = await resp.json()
        assert resp.status == 200
        assert body == {"ok": True, "mutes_opened": True, "changed": True}
        assert slot.mutes_opened is True


class TestMutePersistBeforePublish:
    @pytest.mark.asyncio
    async def test_live_flag_stays_prior_during_save_and_flips_only_after_commit(self) -> None:
        """The live flag must NOT change while the save runs: the new value is
        passed as a staged override, and the live flag is flipped only when the
        save invokes its ``after_commit_under_lock`` hook (which the handler
        wires to set ``slot.mutes_opened``)."""
        slot = _owned_slot("chat-1-100")  # prior = False
        state = _state(slot)
        observed: dict[str, object] = {}

        async def _fake_save(*args, **kwargs):
            # While the save is in flight the live flag must still be the
            # COMMITTED value, and the new value must travel as the staged
            # override, not on the live slot.
            observed["live_during_save"] = slot.mutes_opened
            observed["override"] = kwargs.get("mutes_opened_override")
            cb = kwargs.get("after_commit_under_lock")
            observed["has_callback"] = callable(cb)
            # Simulate the write committing under the lock: the hook flips the
            # live flag, exactly as it runs inside the transcript _locked block.
            observed["live_before_cb"] = slot.mutes_opened
            cb()
            observed["live_after_cb"] = slot.mutes_opened
            return True

        f1, f2, f3, f4, f5 = _pass_all_fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _fake_save),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
        assert resp.status == 200
        assert observed["live_during_save"] is False  # NOT flipped before the save
        assert observed["override"] is True  # new value staged
        assert observed["has_callback"] is True
        assert observed["live_before_cb"] is False  # still prior just before commit
        assert observed["live_after_cb"] is True  # flipped by the after-commit hook
        assert slot.mutes_opened is True
        # Published only after the save returned (and only once).
        state.push_slot_patch.assert_called_once_with("chat-1-100", ("mutes_opened",))

    @pytest.mark.asyncio
    async def test_concurrent_broadcast_during_save_sees_the_old_value(self) -> None:
        """A worker-completion broadcast / slots GET that reads the live flag
        DURING the save window must observe the committed prior value, never the
        provisional one -- that is the whole point of not flipping until commit."""
        slot = _owned_slot("chat-1-100")  # prior = False
        state = _state(slot)
        seen_by_concurrent_reader: dict[str, object] = {}

        async def _fake_save(*args, **kwargs):
            # Model a concurrent reader snapshotting the live flag mid-save (the
            # projection reads slot.mutes_opened directly, under no txn lock).
            seen_by_concurrent_reader["value"] = slot.mutes_opened
            kwargs["after_commit_under_lock"]()
            return True

        f1, f2, f3, f4, f5 = _pass_all_fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _fake_save),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
        assert resp.status == 200
        assert seen_by_concurrent_reader["value"] is False  # old value during save

    @pytest.mark.asyncio
    async def test_failed_save_changes_nothing_and_publishes_nothing(self) -> None:
        """A raising save (lock timeout / disk full) committed nothing, so the
        live flag is untouched and no corrective frame is published."""
        slot = _owned_slot("chat-1-100")  # prior = False
        state = _state(slot)
        f1, f2, f3, f4, f5 = _pass_all_fences()

        async def _boom(*args, **kwargs):
            # The write never commits, so the after-commit hook never runs.
            raise RuntimeError("disk full")

        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _boom),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
                body = await resp.json()
        assert resp.status == 503
        assert body["code"] == "mutes_opened_save_failed"
        assert slot.mutes_opened is False  # never flipped
        assert slot._dirty is True  # a flush may reconverge the (unchanged) record
        state.push_slot_patch.assert_not_called()  # nothing provisional was published

    @pytest.mark.asyncio
    async def test_refused_save_changes_nothing_and_publishes_nothing(self) -> None:
        """A non-raising refuse (slot deleted/rebound under the write) committed
        nothing: the hook never ran, the live flag is untouched, no frame sent."""
        slot = _owned_slot("chat-1-100")  # prior = False
        state = _state(slot)
        f1, f2, f3, f4, f5 = _pass_all_fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch(
                "kiro_crew.dashboard.chat_folders.save_slot_off_loop",
                AsyncMock(return_value=False),
            ),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "session_gone"
        assert slot.mutes_opened is False  # never flipped
        state.push_slot_patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_lineless_slot_skipped_save_is_not_acknowledged(self) -> None:
        """A line-less / incognito slot has no metadata record, so the
        empty-window merge is a by-design SKIP: ``save_slot_off_loop`` returns
        True but never invokes the after-commit hook (nothing committed). The
        handler must NOT acknowledge that as a durable change -- it returns the
        409 'cannot be saved yet' shape, leaves the live flag untouched, and
        publishes nothing (the commit-witness fix)."""
        slot = _owned_slot("chat-1-100")  # prior = False

        async def _skip(*args, **kwargs):
            # True return, but the after-commit hook is NEVER called -- exactly
            # what the empty-window merge of a line-less tab does.
            return True

        state = _state(slot)
        f1, f2, f3, f4, f5 = _pass_all_fences()
        with (
            f1,
            f2,
            f3,
            f4,
            f5,
            patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", _skip),
        ):
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1-100/mutes-opened", json={"mutes_opened": True}
                )
                body = await resp.json()
        assert resp.status == 409
        assert body["code"] == "session_gone"
        assert slot.mutes_opened is False  # never flipped -- the mute did not take
        state.push_slot_patch.assert_not_called()  # nothing provisional was published


class TestStagedValueReachesBothSavePaths:
    """The staged override must be honored by the codec in BOTH the full line and
    the empty-window merge, so a message-less newborn persists the new value even
    though the live flag has not flipped (the round-1 F1 gap). This exercises the
    codec directly -- the layer both ``build_full_line`` and ``merge_empty_window``
    funnel the field through."""

    def test_full_line_form_honors_the_staged_override(self) -> None:
        from kiro_crew.dashboard.slot_persistence import metadata_codec as mc

        slot = _owned_slot("chat-1-100")  # live flag False
        # Live flag is False, but the staged override is True: the serialized
        # line must carry the staged value, not the live one.
        folds = mc.SaveFolds(memory_mode="persistent", mutes_opened=True)
        line = mc.encode(slot, folds=folds)
        assert line.get("mutes_opened") is True

    def test_empty_window_merge_form_honors_the_staged_override(self) -> None:
        from kiro_crew.dashboard.slot_persistence import metadata_codec as mc

        slot = _owned_slot("chat-1-100")  # live flag False
        folds = mc.SaveFolds(memory_mode="persistent", mutes_opened=True)
        merged = mc.encode(slot, merge=True, folds=folds)
        # The merge form writes the key even when falsy, so a True override must
        # appear as True (round-1: this branch ignored the override entirely).
        assert merged.get("mutes_opened") is True

    def test_no_override_reads_the_live_flag(self) -> None:
        from kiro_crew.dashboard.slot_persistence import metadata_codec as mc

        slot = _owned_slot("chat-1-100")
        slot.mutes_opened = True
        # Default fold (mutes_opened=None) means "read the live slot" -- the
        # behavior every other save relies on.
        folds = mc.SaveFolds(memory_mode="persistent")
        assert mc.encode(slot, folds=folds).get("mutes_opened") is True
        slot.mutes_opened = False
        assert mc.encode(slot, folds=folds).get("mutes_opened", "omitted") == "omitted"
