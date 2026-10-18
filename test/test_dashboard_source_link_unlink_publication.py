"""The source-link unlink handler across its awaits, and what it publishes.

Every await rechecks the slot's binding before the handler writes, and a dismissal
is published only once it commits: a cancel waits for the transaction to settle, a
rolled-back dismissal never hides the chip, and every broadcast goes through one
publication boundary. Shared links, slots and request builders live in
``dashboard_source_link_unlink_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from dashboard_source_link_unlink_helpers import (
    PR_A,
    PR_B,
    _identity_key,
    _request,
    _slot,
)

from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_link_unlink
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.wait import await_until


class TestAwaitBindingRegressions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("read_number", [1, 2], ids=["pin", "fold"])
    @pytest.mark.parametrize("change", ["rebind", "replace", "app"])
    async def test_metadata_read_rechecks_binding(self, monkeypatch, read_number, change):
        import asyncio

        primary = _slot()
        primary._app = "owned"
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary}, app="owned")
        state = req.app["state"]
        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncio.to_thread
        reads = 0

        async def held_read(func, *args, **kwargs):
            nonlocal reads
            result = await original(func, *args, **kwargs)
            if func == state.conversation_log.get_metadata_status:
                reads += 1
                if reads == read_number:
                    entered.set()
                    # flake-ok: the stub parks the handler here until the test sets release below, and the task is reaped through await_until(task)
                    await release.wait()
            return result

        monkeypatch.setattr(asyncio, "to_thread", held_read)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            task = asyncio.create_task(api_chat_slot_source_link_unlink(req))
            await await_until(entered)
            if change == "rebind":
                primary.linked_session_key = "dashboard:foreign"
            elif change == "replace":
                state._slots["s1"] = _slot()
            else:
                primary._app = "foreign"
            release.set()
            response = await await_until(task)
        assert response.status in (404, 409)
        state.conversation_log.update_metadata_if.assert_not_called()
        assert key not in primary._dismissed_source_links
        assert key not in state._slots["s1"]._dismissed_source_links
        assert primary._dismissed_txn_depth == 0

    @pytest.mark.asyncio
    async def test_fold_read_never_copies_other_tombstones_to_a_departed_alias(self, monkeypatch):
        import asyncio

        primary, sibling = _slot(), _slot()
        sibling.key = "s2"
        sibling.linked_session_key = "dashboard:s1"
        key, other = _identity_key(PR_A), _identity_key(PR_B)
        req = _request("s1", key, {"s1": primary, "s2": sibling})
        state = req.app["state"]
        state.conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": [other]},
            True,
        )
        original = asyncio.to_thread
        reads = 0

        async def rebind_on_fold(func, *args, **kwargs):
            nonlocal reads
            result = await original(func, *args, **kwargs)
            if func == state.conversation_log.get_metadata_status:
                reads += 1
                if reads == 2:
                    sibling.linked_session_key = "dashboard:foreign"
            return result

        monkeypatch.setattr(asyncio, "to_thread", rebind_on_fold)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            response = await api_chat_slot_source_link_unlink(req)
        assert response.status == 200
        assert other in primary._dismissed_source_links
        assert other not in sibling._dismissed_source_links
        assert key not in sibling._dismissed_source_links

    @pytest.mark.asyncio
    async def test_reconciliation_read_cannot_borrow_another_transcripts_key(self, monkeypatch):
        import asyncio

        primary, sibling = _slot(), _slot()
        sibling.key = "s2"
        sibling.linked_session_key = "dashboard:s1"
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary, "s2": sibling})
        state = req.app["state"]
        original = asyncio.to_thread

        def metadata(hkey):
            return (
                {
                    "_type": "metadata",
                    "created_at": "t",
                    "dismissed_source_links": [key] if hkey == "dashboard:B" else [],
                },
                True,
            )

        state.conversation_log.get_metadata_status.side_effect = metadata

        async def rebind_during_reads(func, *args, **kwargs):
            result = await original(func, *args, **kwargs)
            if func == state.conversation_log.update_metadata_if:
                sibling.linked_session_key = "dashboard:B"
            elif sibling.linked_session_key == "dashboard:B":
                sibling.linked_session_key = "dashboard:C"
            return result

        monkeypatch.setattr(asyncio, "to_thread", rebind_during_reads)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            response = await api_chat_slot_source_link_unlink(req)
        assert response.status == 200
        assert key not in sibling._dismissed_source_links
        assert sibling._dismissed_txn_depth == 0

    @pytest.mark.asyncio
    async def test_broadcast_exception_emits_secret_free_failed_audit(self):
        primary = _slot()
        req = _request("s1", _identity_key(PR_A), {"s1": primary})
        state = req.app["state"]
        # Persist-before-publish: make the durable write land so the broadcast
        # phase is reached, then have the (post-persist, non-aborting) broadcast
        # raise. The failure is audited secret-free and swallowed — no
        # RuntimeError propagates because disk already committed.
        state.conversation_log.get_metadata_status.side_effect = lambda _h: (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": []},
            True,
        )
        state.conversation_log.update_metadata_if.side_effect = lambda _h, _f, _g: True
        state.push_slots_update.side_effect = RuntimeError("sensitive diagnostic")
        with patch("kiro_crew.dashboard.chat_handlers.sel") as audit:
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        events = audit.return_value.log_tool_invocation.call_args_list
        # A broadcast-phase failed audit is emitted, secret-free.
        broadcast_fail = [
            e.kwargs
            for e in events
            if e.kwargs.get("outcome") == "failed"
            and e.kwargs.get("metadata", {}).get("phase") == "broadcast"
        ]
        assert broadcast_fail
        assert "sensitive diagnostic" not in str(events)
        assert primary._dismissed_txn_depth == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["none", "rebind", "replace", "hydrate"])
    async def test_cron_retries_only_the_same_unhydrated_binding(self, monkeypatch, change):
        import asyncio

        from kiro_crew.dashboard.cron_inject import _DISMISSED_UNREAD, prefetch_cron_dismissed

        slot = _ChatSlot("cron-job")
        slot.linked_session_key = "cron:job"
        slot._dismissed_hydrated = False
        key, other = _identity_key(PR_A), _identity_key(PR_B)
        state = MagicMock()
        state.get_slot.side_effect = lambda name: slot if name == "cron-job" else None
        state.conversation_log.get_metadata_status.return_value = (
            {"_type": "metadata", "created_at": "t", "dismissed_source_links": [key]},
            True,
        )
        original = asyncio.to_thread

        async def change_binding(func, *args, **kwargs):
            result = await original(func, *args, **kwargs)
            if change == "rebind":
                slot.linked_session_key = "cron:other"
            elif change == "replace":
                state.get_slot.side_effect = lambda name: _ChatSlot("cron-job")
            elif change == "hydrate":
                slot._dismissed_source_links = {other}
                slot._dismissed_hydrated = True
            return result

        monkeypatch.setattr(asyncio, "to_thread", change_binding)
        result = await prefetch_cron_dismissed(state, "job")
        state.conversation_log.get_metadata_status.assert_called_once_with("cron:job")
        if change == "none":
            assert slot._dismissed_hydrated
            assert slot._dismissed_source_links == {key}
        else:
            assert result is _DISMISSED_UNREAD
            assert key not in slot._dismissed_source_links
        if change == "hydrate":
            assert slot._dismissed_source_links == {other}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["confirm", "recheck"])
    async def test_later_await_rechecks_an_already_reconciled_alias(self, monkeypatch, phase):
        import asyncio

        primary, sibling, joiner = _slot(), _slot(), _slot()
        sibling.key, joiner.key = "s2", "s3"
        sibling.linked_session_key = "dashboard:s1"
        joiner.linked_session_key = "dashboard:elsewhere"
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary, "s2": sibling, "s3": joiner})
        state = req.app["state"]
        original = asyncio.to_thread
        writes = 0
        original_reads = 0

        def metadata(hkey):
            return (
                {
                    "_type": "metadata",
                    "created_at": "t",
                    "dismissed_source_links": [key] if hkey == "dashboard:B" or writes else [],
                },
                True,
            )

        state.conversation_log.get_metadata_status.side_effect = metadata

        async def move_on_later_await(func, *args, **kwargs):
            nonlocal writes, original_reads
            result = await original(func, *args, **kwargs)
            if func == state.conversation_log.update_metadata_if:
                writes += 1
                if writes == 1:
                    sibling.linked_session_key = "dashboard:B"
                    joiner.linked_session_key = "dashboard:s1"
                if phase == "confirm" and writes == 2:
                    sibling.linked_session_key = "dashboard:C"
                # Confirm fails (writes==2) unless we're exercising the confirm
                # phase itself; a failed confirm drops to accept-committed.
                return writes == 1 or phase == "confirm"
            if args == ("dashboard:s1",):
                original_reads += 1
                # The ``recheck`` phase rebinds the sibling during the LAST
                # accept-committed identity re-read, after the confirm failed.
                if phase == "recheck" and original_reads == 3:
                    sibling.linked_session_key = "dashboard:C"
            if args == ("dashboard:C",):
                return ({"_type": "metadata", "created_at": "c"}, True)
            return result

        monkeypatch.setattr(asyncio, "to_thread", move_on_later_await)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            response = await api_chat_slot_source_link_unlink(req)
        assert response.status == 200
        assert sibling.linked_session_key == "dashboard:C"
        assert key not in sibling._dismissed_source_links
        assert all(s._dismissed_txn_depth == 0 for s in (primary, sibling, joiner))

    @pytest.mark.asyncio
    async def test_a_later_target_read_cannot_rebind_an_already_checked_alias(self, monkeypatch):
        import asyncio

        primary, first, second = _slot(), _slot(), _slot()
        first.key, second.key = "s2", "s3"
        first.linked_session_key = second.linked_session_key = "dashboard:s1"
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary, "s2": first, "s3": second})
        state = req.app["state"]
        original = asyncio.to_thread

        async def move_during_second_read(func, *args, **kwargs):
            result = await original(func, *args, **kwargs)
            if func == state.conversation_log.update_metadata_if:
                first.linked_session_key, second.linked_session_key = "dashboard:B", "dashboard:D"
            elif args == ("dashboard:B",):
                return ({"dismissed_source_links": [key]}, True)
            elif args == ("dashboard:D",):
                first.linked_session_key = "dashboard:C"
            return result

        monkeypatch.setattr(asyncio, "to_thread", move_during_second_read)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            response = await api_chat_slot_source_link_unlink(req)
        assert response.status == 200
        assert key not in first._dismissed_source_links
        assert key not in second._dismissed_source_links

    @pytest.mark.asyncio
    async def test_cron_run_start_recovers_after_transient_metadata_failure(self):
        from kiro_crew.dashboard.cron_inject import ensure_cron_slot

        slot = _ChatSlot("cron-job")
        slot.linked_session_key = "cron:job"
        slot.append("assistant", PR_A)
        slot._dismissed_hydrated = False
        key = _identity_key(PR_A)
        state = MagicMock()
        state.get_slot.return_value = slot
        state.conversation_log.get_metadata_status.side_effect = [
            ({}, False),
            ({"_type": "metadata", "created_at": "t", "dismissed_source_links": [key]}, True),
        ]
        job = MagicMock(id="job", persistent_session=True, hide_in_chat=False)
        await ensure_cron_slot(state, job)
        assert not slot._dismissed_hydrated
        assert slot._pr_source_links()
        await ensure_cron_slot(state, job)
        assert slot._dismissed_hydrated
        assert not slot._pr_source_links()
        await ensure_cron_slot(state, job)
        assert state.conversation_log.get_metadata_status.call_count == 2
        state.conversation_log.read_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_is_durable_on_the_authorized_transcript(self, tmp_path):
        from kiro_crew.history import ConversationLog

        primary = _slot()
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary})
        state = req.app["state"]
        state.conversation_log = ConversationLog(tmp_path / "sessions")
        state.conversation_log.update_metadata("dashboard:s1", {"title": "keep me"})
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            response = await api_chat_slot_source_link_unlink(req)
        assert response.status == 200
        meta, readable = state.conversation_log.get_metadata_status("dashboard:s1")
        assert readable and key in meta["dismissed_source_links"]
        assert meta["title"] == "keep me"

    @pytest.mark.asyncio
    async def test_committed_first_write_is_accepted_when_confirm_fails(self, monkeypatch):
        # Terminal-on-first-commit: the first write commits, the confirm FAILS,
        # and the handler accepts the committed grow-only tombstone as durable
        # (200) rather than running a compensating strip that could erase another
        # gateway's same-key commit. An alias that rebinds BACK onto the
        # authorized transcript during the accept-committed awaits is reconciled,
        # and every txn depth returns to 0.
        import asyncio

        primary, sibling, joiner = _slot(), _slot(), _slot()
        sibling.key, joiner.key = "s2", "s3"
        sibling.linked_session_key = "dashboard:s1"
        joiner.linked_session_key = "dashboard:elsewhere"
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary, "s2": sibling, "s3": joiner})
        state = req.app["state"]
        original = asyncio.to_thread
        writes = 0

        async def move_back(func, *args, **kwargs):
            nonlocal writes
            result = await original(func, *args, **kwargs)
            if func == state.conversation_log.update_metadata_if:
                writes += 1
                if writes == 1:
                    # First write commits; a joiner appears so a confirm follows.
                    joiner.linked_session_key = "dashboard:s1"
                    return True
                # Confirm fails -> accept the committed first write (no compensate).
                return False
            return result

        monkeypatch.setattr(asyncio, "to_thread", move_back)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            response = await api_chat_slot_source_link_unlink(req)
        assert response.status == 200  # committed first write accepted as terminal
        assert writes == 2  # first write + failed confirm only; NO compensation write
        assert all(s._dismissed_txn_depth == 0 for s in (primary, sibling, joiner))


class TestUnlinkPublicationOutcome:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "phase", ["broadcast", "reconcile", "late_alias", "rollback", "accept_committed"]
    )
    async def test_broadcast_failure_keeps_response_disk_and_depth_consistent(
        self, tmp_path, monkeypatch, phase
    ):
        import asyncio

        from kiro_crew.history import ConversationLog

        key = _identity_key(PR_A)
        primary, sibling, joiner = _slot(), _slot(), _slot()
        sibling.key, joiner.key = "s2", "s3"
        sibling.linked_session_key = "dashboard:s1"
        joiner.linked_session_key = "dashboard:elsewhere"
        req = _request("s1", key, {"s1": primary, "s2": sibling, "s3": joiner})
        state = req.app["state"]
        log = ConversationLog(tmp_path / "sessions")
        state.conversation_log = log
        await asyncio.to_thread(log.update_metadata, "dashboard:s1", {"title": "unchanged"})
        original = asyncio.to_thread
        writes = 0
        pushes = 0
        # Persist-before-publish: the first broadcast fires only AFTER the first
        # durable write commits. Every publication is now post-persist, so a
        # broadcast failure never unwinds a committed write — it is audited
        # (secret-free) and swallowed, and the response reflects whether disk
        # committed. ``fail_at`` targets the Nth push under the new ordering.
        fail_at = {
            "broadcast": 1,
            "late_alias": 2,
            "reconcile": 2,
            "rollback": 1,
            "accept_committed": 2,
        }[phase]

        def broadcast():
            nonlocal pushes
            pushes += 1
            if pushes == fail_at:
                raise RuntimeError("private diagnostic must never enter SEL")

        async def drive(func, *args, **kwargs):
            nonlocal writes
            if func == log.update_metadata_if:
                writes += 1
                if phase == "rollback" or (phase == "accept_committed" and writes > 1):
                    return False
            result = await original(func, *args, **kwargs)
            if func == log.update_metadata_if and writes == 1:
                if phase == "reconcile":
                    sibling.linked_session_key = "dashboard:foreign"
                if phase in ("late_alias", "accept_committed"):
                    joiner.linked_session_key = "dashboard:s1"
            return result

        monkeypatch.setattr(asyncio, "to_thread", drive)
        state.push_slots_update.side_effect = broadcast
        with patch("kiro_crew.dashboard.chat_handlers.sel") as audit:
            # No publication is optimistic anymore: a post-persist broadcast is
            # non-aborting, so the handler always returns a response (never a
            # propagated RuntimeError).
            response = await api_chat_slot_source_link_unlink(req)
            status = response.status
        meta, readable = await original(log.get_metadata_status, "dashboard:s1")
        assert readable
        committed = key in meta.get("dismissed_source_links", [])
        assert committed == (status == 200)
        assert (key in primary._dismissed_source_links) == committed
        assert all(s._dismissed_txn_depth == 0 for s in (primary, sibling, joiner))
        events = [call.kwargs for call in audit.return_value.log_tool_invocation.call_args_list]
        # A broadcast delivery failure is audited secret-free, whatever its phase.
        assert any(
            e["outcome"] == "failed" and e.get("error") == "broadcast_failed" for e in events
        )
        assert any(e["outcome"] == "allowed" for e in events) == committed
        assert "private diagnostic" not in str(events)
        assert pushes >= fail_at

    @pytest.mark.asyncio
    @pytest.mark.parametrize("boundary", ["fold", "persist", "confirm", "reconcile"])
    async def test_cancel_waits_for_transaction_settlement(self, tmp_path, monkeypatch, boundary):
        import asyncio

        from kiro_crew.history import ConversationLog

        key = _identity_key(PR_A)
        primary, sibling, joiner = _slot(), _slot(), _slot()
        sibling.key, joiner.key = "s2", "s3"
        sibling.linked_session_key = "dashboard:s1"
        joiner.linked_session_key = "dashboard:elsewhere"
        req = _request("s1", key, {"s1": primary, "s2": sibling, "s3": joiner})
        state = req.app["state"]
        log = ConversationLog(tmp_path / "sessions")
        state.conversation_log = log
        await asyncio.to_thread(log.update_metadata, "dashboard:s1", {"title": "unchanged"})
        original = asyncio.to_thread
        entered, release = asyncio.Event(), asyncio.Event()
        writes = reads = 0

        async def held(func, *args, **kwargs):
            nonlocal writes, reads
            if func == log.update_metadata_if:
                writes += 1
            else:
                reads += 1
            result = await original(func, *args, **kwargs)
            if func == log.update_metadata_if and writes == 1:
                if boundary == "confirm":
                    joiner.linked_session_key = "dashboard:s1"
                if boundary == "reconcile":
                    sibling.linked_session_key = "dashboard:foreign"
            stop = (
                (boundary == "fold" and reads == 2 and writes == 0)
                or (boundary == "persist" and func == log.update_metadata_if and writes == 1)
                or (boundary == "confirm" and func == log.update_metadata_if and writes == 2)
                or (boundary == "reconcile" and args == ("dashboard:foreign",))
            )
            if stop:
                entered.set()
                # flake-ok: the stub parks the handler here until the test sets release below, and the task is reaped through await_until(task)
                await release.wait()
            return result

        monkeypatch.setattr(asyncio, "to_thread", held)
        with patch("kiro_crew.dashboard.chat_handlers.sel") as audit:
            task = asyncio.create_task(api_chat_slot_source_link_unlink(req))
            await await_until(entered)
            task.cancel()
            # flake-ok: one yield delivers the cancel to the parked handler before release is set, and the task is reaped through await_until(task)
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task)
        meta, readable = await original(log.get_metadata_status, "dashboard:s1")
        committed = key in meta.get("dismissed_source_links", [])
        assert readable and (key in primary._dismissed_source_links) == committed
        assert all(s._dismissed_txn_depth == 0 for s in (primary, sibling, joiner))
        if boundary == "reconcile":
            assert key not in sibling._dismissed_source_links
        events = [call.kwargs for call in audit.return_value.log_tool_invocation.call_args_list]
        assert any(e.get("error") == "request_cancelled" for e in events)

    @pytest.mark.asyncio
    async def test_tentative_dismissal_is_not_published_before_it_commits(
        self, tmp_path, monkeypatch
    ):
        # persist-before-publish, on the BROADCAST path: while the guarded write
        # is in flight the slot already carries the dismissal in its in-memory
        # set, so a CONCURRENT push_slots_update fired by another code path could
        # otherwise serialize the removal to clients before disk records it (and
        # a failed write would then roll it back, leaving a client showing a chip
        # disk still holds). The txn-pending fence makes the source-link
        # projection keep the chip VISIBLE until the write commits.
        import asyncio

        from kiro_crew.history import ConversationLog

        key = _identity_key(PR_A)
        primary = _slot()
        req = _request("s1", key, {"s1": primary})
        state = req.app["state"]
        log = ConversationLog(tmp_path / "sessions")
        state.conversation_log = log
        await asyncio.to_thread(log.update_metadata, "dashboard:s1", {"title": "unchanged"})
        original = asyncio.to_thread
        entered, release = asyncio.Event(), asyncio.Event()
        writes = 0

        async def held(func, *args, **kwargs):
            nonlocal writes
            if func == log.update_metadata_if:
                writes += 1
                # Pause with the write IN FLIGHT (before it commits).
                entered.set()
                # flake-ok: the stub parks the handler here until the test sets release below, and the task is reaped through await_until(task)
                await release.wait()
            return await original(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", held)
        # Record what each broadcast would project for the chip.
        pushes_show_chip: list[bool] = []

        def broadcast():
            pushes_show_chip.append(any(link["url"] == PR_A for link in primary._pr_source_links()))

        state.push_slots_update.side_effect = broadcast
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            task = asyncio.create_task(api_chat_slot_source_link_unlink(req))
            await await_until(entered)
            # Mid-write: a concurrent projection MUST still show the chip, even
            # though the key is already in the slot's in-memory dismissed set.
            assert key in primary._dismissed_source_links
            assert key in primary._dismissed_txn_pending
            assert any(link["url"] == PR_A for link in primary._pr_source_links())
            # Let the write commit and the transaction settle.
            release.set()
            resp = await await_until(task)
        assert resp.status == 200
        # After commit the fence is cleared and the chip is gone.
        assert primary._dismissed_txn_pending == set()
        assert key in primary._dismissed_source_links
        assert not any(link["url"] == PR_A for link in primary._pr_source_links())
        # The commit-time broadcast MUST have projected the chip GONE: the fence
        # is cleared before the success publish, so a post-commit broadcast never
        # subtracts a now-stale pending key and leaves other clients showing a
        # chip disk has already removed.
        assert pushes_show_chip and not any(pushes_show_chip)
        meta, readable = await original(log.get_metadata_status, "dashboard:s1")
        assert readable and key in meta.get("dismissed_source_links", [])

    @pytest.mark.asyncio
    async def test_a_rolled_back_tentative_dismissal_never_hides_the_chip(
        self, tmp_path, monkeypatch
    ):
        # The failure mirror of the test above: when the guarded write fails, the
        # tentative dismissal is rolled back and the chip must have stayed visible
        # the whole time -- a client never saw it removed.
        import asyncio

        from kiro_crew.history import ConversationLog

        key = _identity_key(PR_A)
        primary = _slot()
        req = _request("s1", key, {"s1": primary})
        state = req.app["state"]
        log = ConversationLog(tmp_path / "sessions")
        state.conversation_log = log
        await asyncio.to_thread(log.update_metadata, "dashboard:s1", {"title": "unchanged"})
        # Every push must see the chip present: the removal is never published.
        pushes: list[bool] = []

        def broadcast():
            pushes.append(any(link["url"] == PR_A for link in primary._pr_source_links()))

        state.push_slots_update.side_effect = broadcast
        original = asyncio.to_thread

        async def failing(func, *args, **kwargs):
            if func == log.update_metadata_if:
                return False  # guarded write refuses -> rollback + 409
            return await original(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", failing)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 409
        assert key not in primary._dismissed_source_links  # rolled back
        assert primary._dismissed_txn_pending == set()
        assert any(link["url"] == PR_A for link in primary._pr_source_links())
        # Any broadcast that did fire saw the chip present, never removed.
        assert all(pushes)

    @pytest.mark.asyncio
    async def test_a_concurrent_gateways_committed_sibling_is_folded_before_publish(
        self, tmp_path, monkeypatch
    ):
        # persist-before-publish, multi-gateway: while this request dismisses
        # PR_A, a concurrent gateway sharing the data home commits PR_B in the
        # read-to-lock window. _merge_guard persists A∪B under the lock; this
        # request must fold that persisted set back into ``union`` so the values
        # it installs onto live aliases (and marks hydrated) include B, not just
        # A. Otherwise B is dropped from this gateway's in-memory/published state
        # and resurfaces until a later disk re-read.
        import asyncio

        from kiro_crew.history import ConversationLog

        key_a, key_b = _identity_key(PR_A), _identity_key(PR_B)
        primary = _slot(PR_A, PR_B)
        req = _request("s1", key_a, {"s1": primary})
        state = req.app["state"]
        log = ConversationLog(tmp_path / "sessions")
        state.conversation_log = log
        await asyncio.to_thread(log.update_metadata, "dashboard:s1", {"title": "unchanged"})
        original = asyncio.to_thread
        injected = False

        async def concurrent(func, *args, **kwargs):
            nonlocal injected
            # Just before THIS request's guarded write acquires the lock,
            # simulate a concurrent gateway committing PR_B to the same line.
            if func == log.update_metadata_if and not injected:
                injected = True
                await original(
                    log.update_metadata, "dashboard:s1", {"dismissed_source_links": [key_b]}
                )
            return await original(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", concurrent)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await api_chat_slot_source_link_unlink(req)
        assert resp.status == 200
        # The persisted line holds A∪B, and this request folded B back in, so the
        # live slot's in-memory set carries BOTH and is marked hydrated on the
        # complete set — B does not resurface.
        meta, readable = await original(log.get_metadata_status, "dashboard:s1")
        assert readable
        assert {key_a, key_b} <= set(meta.get("dismissed_source_links", []))
        assert {key_a, key_b} <= primary._dismissed_source_links
        assert primary._dismissed_hydrated
        urls = {link["url"] for link in primary._pr_source_links()}
        assert PR_A not in urls and PR_B not in urls

    @pytest.mark.asyncio
    async def test_repeated_cancel_retains_settlement_and_releases_depth(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.history import ConversationLog

        primary = _slot()
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary})
        state = req.app["state"]
        log = ConversationLog(tmp_path / "sessions")
        state.conversation_log = log
        original = asyncio.to_thread
        await original(log.update_metadata, "dashboard:s1", {})
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_write(func, *args, **kwargs):
            result = await original(func, *args, **kwargs)
            if func == log.update_metadata_if:
                entered.set()
                # flake-ok: the stub parks the settlement write here until the test sets release below, and the settlement is reaped through await_until
                await release.wait()
            return result

        monkeypatch.setattr(asyncio, "to_thread", hold_write)
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            caller = asyncio.create_task(api_chat_slot_source_link_unlink(req))
            await await_until(entered)
            caller.cancel()
            # flake-ok: one yield lets the first cancel reach the parked caller before the second cancel is sent
            await asyncio.sleep(0)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: the caller was cancelled twice above, so this await ends in its CancelledError; the settlement is reaped through await_until below
                await caller
            settlement = tuple(chat_handlers._source_link_unlink_tasks)
            assert len(settlement) == 1 and not settlement[0].cancelled()
            release.set()
            responses = await await_until(asyncio.ensure_future(asyncio.gather(*settlement)))
        assert responses[0].status == 200
        assert primary._dismissed_txn_depth == 0
        assert key in primary._dismissed_source_links
        assert not chat_handlers._source_link_unlink_tasks

    @pytest.mark.asyncio
    async def test_terminal_audit_failure_cannot_overturn_a_commit(self, tmp_path):
        import asyncio

        from kiro_crew.history import ConversationLog

        primary = _slot()
        key = _identity_key(PR_A)
        req = _request("s1", key, {"s1": primary})
        log = ConversationLog(tmp_path / "sessions")
        req.app["state"].conversation_log = log
        await asyncio.to_thread(log.update_metadata, "dashboard:s1", {})
        with patch(
            "kiro_crew.dashboard.chat_handlers.sel", side_effect=OSError("private audit path")
        ):
            response = await api_chat_slot_source_link_unlink(req)
        meta, _ = await asyncio.to_thread(log.get_metadata_status, "dashboard:s1")
        assert response.status == 200 and key in meta["dismissed_source_links"]
        assert primary._dismissed_txn_depth == 0

    def test_every_broadcast_uses_the_single_publication_boundary(self):
        import ast
        import inspect

        from kiro_crew.dashboard.chat_handlers import _apply_source_link_unlink

        tree = ast.parse(inspect.getsource(_apply_source_link_unlink))
        raw = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "push_slots_update"
        ]
        publish = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_publish"
        )
        assert len(raw) == 1 and raw[0] in list(ast.walk(publish))
        phases = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_publish"
        }
        assert phases == {
            "broadcast",
            "reconcile",
            "late_alias",
            "rollback",
            "accept_committed",
            "final",
        }
