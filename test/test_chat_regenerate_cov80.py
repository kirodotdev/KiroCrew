"""Coverage for the guard and failure paths of
:mod:`kiro_crew.dashboard.chat_regenerate`.

``test_dashboard_chat.py::TestRegenerateAndVariants`` covers the happy paths of
regenerate and variant switching. Untested there: ``edit-resend`` in its
entirety (it is not even wired into the shared test app), every 400/404/409
guard on all three endpoints, the readiness latch that must fire BEFORE the
destructive truncation, the persist-failure paths, and the two done-callbacks.

The app here registers the three handlers directly so ``edit-resend`` is
reachable; ``_run_chat`` is always patched, so no backend session is started.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_regenerate
from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
    api_chat_slot_switch_variant,
)

# Ceiling for the cross-thread gates below. Generous rather than tight: it is a
# deadlock backstop, never a synchronisation point, so a slow shared runner must
# not trip it -- every test that uses it also releases its gate in a ``finally``.
_GATE_TIMEOUT_SECS = 30


def _make_regen_app(state) -> web.Application:
    """App exposing all three chat_regenerate routes, including edit-resend."""
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post("/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant)
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    return app


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    # edit-resend is now a real conversation boundary: it discards the native
    # ACP conversation and flushes the cleared resume sid BEFORE persisting the
    # truncated history. Configure the sessions double so the happy paths reach
    # commit -- discard succeeds (returns True), the flush is a no-op, and the
    # orphan-session lookup returns "" so no cleanup is attempted.
    st.sessions.discard_conversation = AsyncMock(return_value=True)
    st.sessions.aflush = AsyncMock()
    st.sessions._session_map.get = MagicMock(return_value="")
    return st


def _client(state):
    return TestClient(TestServer(_make_regen_app(state)))


async def _busy(slot) -> None:
    """Pin the slot as running with a task that outlives the request."""

    async def _sleep() -> None:
        await asyncio.sleep(10)

    slot.task = asyncio.create_task(_sleep())


# ── regenerate ──


@pytest.mark.asyncio
async def test_regenerate_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/nope/regenerate")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_regenerate_requires_a_preceding_user_message(state) -> None:
    """An assistant-first transcript has nothing to re-send."""
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "unprompted greeting")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["error"] == "no preceding user message"
    assert [m["role"] for m in slot.messages] == ["assistant"]  # untouched


@pytest.mark.asyncio
async def test_regenerate_rejects_an_empty_user_message(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "")
    slot.append("assistant", "reply to nothing")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["error"] == "empty user message"


@pytest.mark.asyncio
async def test_regenerate_skips_a_trailing_system_notice(state) -> None:
    """A trailing compaction/session-reload notice is a status row, not the
    reply being regenerated: the variant capture must take the real reply.
    Capturing the notice instead drops the reply from variant history with
    no recovery path."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "the real reply")
    slot.append("assistant", "auto compacted", meta={"kind": "compaction"})
    slot.drain()

    captured: list[list[dict]] = []

    async def _capture(*args, **kwargs) -> None:
        # Runs before the done-callback discards unconsumed variants.
        captured.append(list(slot._pending_variants))

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task

    # The variant stash holds the reply, never the notice.
    assert captured, "background turn never started"
    contents = [v.get("content") for v in captured[0]]
    assert "the real reply" in contents
    assert "auto compacted" not in contents
    # This stub produces no reply, so the done-callback restores the removed
    # rows rather than leaving the transcript truncated. Both the reply and its
    # trailing notice come back in their original order.
    assert [m["role"] for m in slot.messages] == ["user", "assistant", "assistant"]
    assert slot.messages[1]["content"] == "the real reply"
    assert slot.messages[2]["content"] == "auto compacted"


@pytest.mark.asyncio
async def test_regenerate_never_crosses_a_newer_user_turn(state) -> None:
    """The notice skip must stop at a real user row: a /compact command is a
    user row followed by its notice, and skipping past it would regenerate the
    PRIOR turn -- irreversibly deleting the newer user turn. With no reply in
    the newest turn there is nothing to regenerate: refuse, mutate nothing."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "Q")
    slot.append("assistant", "A")
    slot.append("user", "/compact")
    slot.append("assistant", "auto compacted", meta={"kind": "compaction"})
    slot.drain()

    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_assistant_message"

    # Nothing was truncated or persisted.
    assert [m["role"] for m in slot.messages] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_readiness_latch_blocks_before_the_truncation(state) -> None:
    """Regenerate persists the truncation, so an unverified backend must be
    rejected BEFORE history is mutated -- a failed turn cannot undo it."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    blocked = web.json_response({"error": "kiro not verified"}, status=503)

    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=blocked),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")

    assert resp.status == 503
    assert [m["role"] for m in slot.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_regenerate_survives_a_history_write_failure(state, caplog) -> None:
    """A failed truncating write must not fail the request: the endpoint catches
    it, logs it, and dispatches the turn anyway (the in-memory window is the
    source of truth and the periodic flush retries)."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    calls = []

    async def _first_write_fails(*a, **kw):
        # Only the endpoint's truncating write (first call) fails; a later
        # restore write, if any, takes the real path.
        calls.append(kw)
        if len(calls) == 1:
            raise OSError("disk full")
        return True

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_first_write_fails,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                if slot.task is not None:
                    await slot.task
                await asyncio.sleep(0)
                if slot._regenerate_restore_task is not None:
                    await slot._regenerate_restore_task

    # The truncating write failed and was caught; the request still succeeded.
    assert "failed to rewrite session history" in caplog.text
    # Two writes were attempted: the endpoint's truncating write (failed) and
    # the restore's merge write (succeeded) -- the stubbed turn produced no
    # reply, so the done-callback restored the previous reply.
    assert len(calls) == 2
    # The restore ran and grew the window back past the truncation, so it
    # cleared the rewrite flag the truncation armed. The flag is False, not the
    # armed-for-retry True of the write-fails-with-a-reply case below.
    assert slot._pending_rewrite is False


@pytest.mark.asyncio
async def test_regenerate_write_failure_with_a_reply_keeps_the_rewrite_flag(state, caplog) -> None:
    """The original invariant: when the truncating write fails BUT the turn
    produces a reply (so no restore runs and no save commits), _pending_rewrite
    stays True so the periodic flush still archives the dropped tail."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    async def _always_fails(*a, **kw):
        raise OSError("disk full")

    async def _reply_turn(st, sl, *a, **kw):
        # Mimic a turn whose reply landed: _flush_segment consumed the stashed
        # variants and appended the fresh reply. That clears _pending_variants
        # (so the done-callback restores nothing) without committing a durable
        # save (so _pending_rewrite is left as the truncation armed it).
        sl._pending_variants = []
        sl.messages.append({"role": "assistant", "content": "hello v2"})

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_always_fails,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_reply_turn),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                if slot.task is not None:
                    await slot.task
                await asyncio.sleep(0)
                if slot._regenerate_restore_task is not None:
                    await slot._regenerate_restore_task

    # The truncating write failed and was caught; the request still succeeded.
    assert "failed to rewrite session history" in caplog.text
    # A reply landed, so no restore ran and nothing cleared the flag the
    # truncation armed: it stays True so the periodic flush archives the tail.
    assert slot._pending_rewrite is True
    # No restore was scheduled -- the reply consumed the stash.
    assert slot._regenerate_restore_task is None
    assert slot.messages[-1]["content"] == "hello v2"


@pytest.mark.asyncio
async def test_regenerate_rebind_before_restore_rewrites_the_original_transcript(
    state, caplog
) -> None:
    """A rebind to a DIFFERENT conversation BEFORE the restore's top identity
    check must not strand the original transcript. The eager truncation already
    removed and persisted the previous reply from the original transcript; if the
    slot is then rebound and the restore merely returns, that transcript is left
    permanently short of its reply (data loss on a conversation nobody rebound).
    The restore must write the captured rows back to the ORIGINAL transcript
    (restore_expected_key), not the slot's new binding."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    # The real eager truncation persists the transcript, so it exists on disk
    # before recovery; the stubbed save does not write, so seed it so the
    # recovery's "transcript still exists" guard does not read a deleted session.
    state.conversation_log.append(original_key, "user", "hi")

    async def _truncate_commits(*a, **kw):
        # The endpoint's inline truncating save commits: the original transcript
        # durably loses its reply.
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        # The turn produces no reply (so a restore is scheduled), and a cron binds
        # this slot to a DIFFERENT conversation before the done-callback restore
        # runs -> the restore's top identity check sees a different transcript.
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_commits,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            # The write-back is off-loop; let the executor task settle.
            await asyncio.sleep(0.05)

    # The reply was written BACK to the ORIGINAL transcript, not left lost and not
    # written onto the rebound (new) conversation.
    original_rows = state.conversation_log.read_messages(original_key)
    assert any(
        m.get("content") == "ORIGINAL-REPLY" for m in original_rows
    ), "the previous reply must be restored to its ORIGINAL transcript on a rebind"
    other_rows = state.conversation_log.read_messages("dashboard:cron-other-conversation")
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in other_rows
    ), "the previous reply must NOT land on the rebound conversation's transcript"


@pytest.mark.asyncio
async def test_regenerate_respell_undo_splices_before_a_concurrent_append(state, caplog) -> None:
    """When the inline truncating save is refused by a same-file key respelling
    and the retry on the current key also cannot commit, the endpoint UNDOES the
    truncation. If another writer appended a row during the retry save-await,
    re-inserting the old reply at end-of-list would order it AFTER that newer row
    (wrong transcript order). The undo must splice the reply back at the user
    row's anchor so order is preserved: user -> reply -> newer row."""
    slot = state.get_or_create_slot("s1")
    slot.linked_session_key = "slack_1712793600.1"
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()

    saves = []

    async def _truncate_respell_refuse_then_retry_refuse(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            # Inline truncating save: a reconciler respells the key to an
            # equivalent spelling of the SAME transcript; the raw-key guard
            # refuses on the spelling.
            slot.linked_session_key = "slack:1712793600.1"
            return False
        # The retry on the current key: another writer appends a newer row during
        # this await, then the retry still cannot commit -> the endpoint undoes
        # the truncation.
        slot.messages.append({"role": "assistant", "content": "NEWER-ROW", "cls": "msg msg-a"})
        return False

    with patch(
        "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
        new=_truncate_respell_refuse_then_retry_refuse,
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 409

    contents = [m.get("content") for m in slot.messages]
    assert "ORIGINAL-REPLY" in contents, "the undo must keep the previous reply"
    assert "NEWER-ROW" in contents, "the concurrently-appended row must be kept"
    # Order preserved: the restored reply lands at its user-row anchor, BEFORE the
    # concurrently-appended newer row.
    assert contents.index("ORIGINAL-REPLY") < contents.index(
        "NEWER-ROW"
    ), "the undone reply must splice at its anchor, not after a concurrent append"


@pytest.mark.asyncio
async def test_regenerate_recovery_preserves_variants_and_metadata(state, caplog) -> None:
    """The rebind recovery write to the original transcript must preserve the
    reply's variant history and metadata (meta.mid), not lossy-convert it. A
    reply with prior variants that is recovered to its original transcript must
    land there WITH its variants and id intact so the user can still switch
    back to a prior variant after a replay."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    # A reply carrying variant history + a stable id, exactly what a real
    # regenerated reply holds.
    slot.messages.append(
        {
            "role": "assistant",
            "content": "REPLY-V2",
            "cls": "msg msg-a",
            "meta": {"mid": "mid-reply-1"},
            "variants": [{"role": "assistant", "content": "REPLY-V1"}, {"content": "REPLY-V2"}],
            "variant_idx": 1,
        }
    )
    slot.drain()
    original_key = slot_history_key(slot)
    # The real eager truncation persists the (truncated) transcript, so the file
    # exists on disk before recovery runs. The stubbed save below does not write,
    # so seed the transcript so the recovery's "transcript still exists" guard
    # sees a present file rather than treating it as a deleted session.
    state.conversation_log.append(original_key, "user", "hi")

    async def _truncate_commits(*a, **kw):
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task

    rows = state.conversation_log.read_messages(original_key)
    reply = next((m for m in rows if m.get("content") == "REPLY-V2"), None)
    assert reply is not None, "the reply must be recovered to the original transcript"
    # Variants and metadata survived — not lossy-converted away.
    assert (
        isinstance(reply.get("variants"), list) and len(reply["variants"]) == 2
    ), "variant history must survive the recovery write"
    assert reply.get("variant_idx") == 1, "variant_idx must survive the recovery write"
    assert reply.get("meta", {}).get("mid") == "mid-reply-1", "meta.mid must survive the recovery"


@pytest.mark.asyncio
async def test_regenerate_recovery_write_failure_keeps_recovery_retryable(state, caplog) -> None:
    """When the durable recovery write to the original transcript FAILS, the
    recovery must stay retryable — the slot keeps _regenerate_restore_pending set
    (so it is fenced, not settled) and the in-memory recovery is not dropped —
    rather than fire-and-forget-settling and losing the only recoverable copy.
    A subsequent retry that succeeds then clears the marker."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()

    async def _truncate_commits(*a, **kw):
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    writes = {"n": 0}

    async def _recovery_fails_first_then_succeeds(conversation_log, key, rows, **_kw):
        # First recovery attempt fails (write could not commit); the retry
        # succeeds. The recovery must NOT settle on the first failure.
        writes["n"] += 1
        if writes["n"] == 1:
            return False
        # Actually persist on the retry so the reply is recoverable.
        for row in rows:
            conversation_log.append_full_message_if_absent(key, row)
        return True

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_fails_first_then_succeeds,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            # The first write failed -> a retry task was re-armed as the restore
            # task. Drain it (and any successor) to completion.
            for _ in range(5):
                t = slot._regenerate_restore_task
                if t is None:
                    break
                await t
                await asyncio.sleep(0)

    # The recovery was retried, not settled on the first failure.
    assert writes["n"] >= 2, "a failed recovery write must be retried, not fire-and-forget-settled"
    # The retry committed, so the reply is on the original transcript and the
    # slot's restore-pending fence is released.
    original_rows = state.conversation_log.read_messages("dashboard:s1")
    assert any(
        m.get("content") == "ORIGINAL-REPLY" for m in original_rows
    ), "the retried recovery must land the reply on the original transcript"
    assert (
        slot._regenerate_restore_pending is False
    ), "a confirmed retry must release the restore-pending fence"


@pytest.mark.asyncio
async def test_regenerate_recovery_retry_is_bounded_and_releases_the_fence(
    state, caplog, monkeypatch
) -> None:
    """A durable recovery write that can NEVER commit (a full / read-only data
    home) must not re-arm the retry forever at 4Hz. Each re-arm reinstalls
    slot._regenerate_restore_task before the settlement guard runs, so an
    unbounded loop pins _regenerate_restore_pending permanently and wedges the
    slot into a slot_restoring 409 on every regenerate/edit-resend/switch-variant
    and makes _close_slot raise forever. After a BOUNDED number of attempts the
    retry stops, LEAVES the _PENDING_RECOVERIES entry for the shutdown drain, and
    RELEASES the fences so ordinary operations are not blocked."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()
    # Fast retries so the bounded sequence completes within the test.
    monkeypatch.setattr(_cr, "_RECOVERY_RETRY_DELAY_SECS", 0.0)

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    async def _truncate_commits(*a, **kw):
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    writes = {"n": 0}

    async def _recovery_always_fails(conversation_log, key, rows, **_kw):
        writes["n"] += 1
        return False

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_always_fails,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            # Drain the retry chain to its bounded end.
            for _ in range(40):
                t = slot._regenerate_restore_task
                if t is None:
                    break
                try:
                    await t
                except Exception:
                    pass
                await asyncio.sleep(0)

    # Bounded: the retry stopped re-arming (it did NOT loop forever). The write
    # count is finite and small (first attempt + the bounded retries).
    assert (
        1 <= writes["n"] <= _cr._RECOVERY_DRAIN_ATTEMPTS + 2
    ), f"the recovery retry must be bounded, not loop forever (writes={writes['n']})"
    # The fence is RELEASED so the slot is not wedged into a permanent 409.
    assert slot._regenerate_restore_task is None, "the retry chain must stop re-arming the task"
    assert (
        slot._regenerate_restore_pending is False
    ), "a bounded-exhausted retry must release the restore-pending fence, not wedge the slot"
    # The reply is still OWED a durable write, so the entry is LEFT for the drain.
    assert any(
        v[1] == original_key for v in _cr._PENDING_RECOVERIES.values()
    ), "an exhausted retry must leave the pending entry for the shutdown drain"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_mid_save_rebind_does_not_lose_the_reply(state, caplog) -> None:
    """A committed truncation then a rebind during the restore's merge save must
    not lose the reply: the restore recovers it DURABLY to the original
    transcript before stripping the rows off the rebound slot. The reply ends on
    the original transcript and NOT on the rebound conversation."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    # The real eager truncation persists the transcript, so it exists on disk
    # before recovery; the stubbed save does not write, so seed it so the
    # recovery's "transcript still exists" guard does not read a deleted session.
    state.conversation_log.append(original_key, "user", "hi")

    saves = []

    async def _truncate_ok_then_rebind_and_commit(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True  # endpoint truncating write commits
        # The restore's merge write: slot is rebound to a DIFFERENT conversation
        # during the await, and the save still commits (its pin matched the old
        # key when it ran).
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return True

    async def _empty_turn(*a, **kw):
        return None

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_commit,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            await asyncio.sleep(0.05)

    # The reply was recovered to the ORIGINAL transcript (not lost), and the
    # rebound slot was stripped of it so it cannot leak into the new conversation.
    original_rows = state.conversation_log.read_messages(original_key)
    assert any(
        m.get("content") == "ORIGINAL-REPLY" for m in original_rows
    ), "a mid-save rebind must recover the reply to its original transcript, not lose it"
    other_rows = state.conversation_log.read_messages("dashboard:cron-other-conversation")
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in other_rows
    ), "the reply must not leak onto the rebound conversation"
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in slot.messages
    ), "the reply must be stripped off the rebound slot's live window"


@pytest.mark.asyncio
async def test_regenerate_delete_during_regenerate_does_not_recreate_transcript(
    state, caplog
) -> None:
    """A History delete DURING regenerate must be honored, not undone by the
    recovery write. If the original transcript is deleted while the slot is
    rebound, the recovery must SETTLE WITHOUT recreating it — recreating would
    resurrect a session the user deleted. The reply is dropped (the deletion is
    the user's intent), and the transcript file stays gone."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    # Seed the transcript so it genuinely exists before the delete (the real
    # eager truncation would have persisted it).
    state.conversation_log.append(original_key, "user", "hi")
    assert state.conversation_log._path(original_key).exists()

    saves = []

    async def _truncate_ok_then_rebind_and_delete(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True  # endpoint truncating write commits
        # The restore's merge write: the user deletes the session mid-regenerate
        # (transcript unlinked) AND the slot is rebound. The save refuses because
        # the slot moved off the original key.
        state.conversation_log.delete_session(original_key)
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_delete,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            await asyncio.sleep(0.05)

    # The deleted transcript was NOT recreated by the recovery write.
    assert not state.conversation_log._path(
        original_key
    ).exists(), "a delete during regenerate must not be undone by the recovery write"
    # The recovery settled (no endless retry re-arming): the slot is not left
    # fenced forever on a session that is gone.
    assert (
        slot._regenerate_restore_pending is False
    ), "a honored deletion must settle the recovery, not keep it pending"


@pytest.mark.asyncio
async def test_recovery_to_absent_transcript_logs_instead_of_silent_discard(
    tmp_path, caplog
) -> None:
    """A recovery targeting an ABSENT transcript (deleted mid-regenerate, OR a
    truncation that never persisted) must not SILENTLY settle: it must emit a
    WARNING naming the key so a possible never-persisted discard is observable.
    It must also NOT recreate the file (honoring a possible delete)."""
    from pathlib import Path

    from kiro_crew.history import ConversationLog, restore_full_rows_off_loop

    sdir = tmp_path / "sessions"
    sdir.mkdir()
    log = ConversationLog(base_dir=Path(sdir))
    key = "dashboard:absent-conv"
    # The transcript does NOT exist (never persisted / deleted).
    assert not log._path(key).exists()

    rows = [{"role": "assistant", "content": "LOST-REPLY", "cls": "msg msg-a"}]
    with caplog.at_level("WARNING"):
        committed = await restore_full_rows_off_loop(log, key, rows)

    # The recovery settles (nothing on disk to retry against) but is NOT silent:
    # a WARNING names the key, and the absent transcript is not recreated.
    assert committed is True, "recovery settles on an absent transcript (nothing to retry against)"
    assert not log._path(
        key
    ).exists(), "recovery must not recreate an absent (possibly deleted) transcript"
    assert any(
        "absent at recovery time" in r.getMessage() and key in r.getMessage()
        for r in caplog.records
    ), "settling on an absent transcript must emit a WARNING naming the key, not discard silently"


@pytest.mark.asyncio
async def test_recovery_absence_honored_only_when_transcript_existed_at_truncation(
    tmp_path, caplog
) -> None:
    """The delete-vs-never-persisted ambiguity is resolved by the recovery
    record's existed_at_truncation bit. A transcript that NEVER existed at
    truncation (existed_at_truncation=False) and is absent now was never
    deleted — its truncation simply never persisted — so recovery RECREATES it
    with the reply rather than discarding a real reply. A transcript that DID
    exist at truncation (existed_at_truncation=True) and is absent now was
    DELETED, so recovery honors the delete and does NOT recreate it."""
    from pathlib import Path

    from kiro_crew.history import ConversationLog, restore_full_rows_off_loop

    sdir = tmp_path / "sessions"
    sdir.mkdir()
    log = ConversationLog(base_dir=Path(sdir))
    rows = [{"role": "assistant", "content": "REAL-REPLY", "cls": "msg msg-a"}]

    # Case 1 — NEVER PERSISTED: absent + existed_at_truncation=False. The reply
    # is a real loss to recover, so the transcript is RECREATED with it.
    never_key = "dashboard:never-persisted"
    assert not log._path(never_key).exists()
    committed = await restore_full_rows_off_loop(log, never_key, rows, existed_at_truncation=False)
    assert committed is True
    assert log._path(never_key).exists(), (
        "a never-persisted (existed_at_truncation=False) absent transcript must be RECREATED "
        "with the recovered reply, not silently discarded"
    )
    recreated = log.read_messages(never_key)
    assert any(
        m.get("content") == "REAL-REPLY" for m in recreated
    ), "the recovered reply must be durably written when the transcript never existed"

    # Case 2 — DELETED: absent + existed_at_truncation=True. The delete is
    # honored; the transcript is NOT recreated.
    deleted_key = "dashboard:deleted-mid-regen"
    assert not log._path(deleted_key).exists()
    committed2 = await restore_full_rows_off_loop(
        log, deleted_key, rows, existed_at_truncation=True
    )
    assert committed2 is True
    assert not log._path(deleted_key).exists(), (
        "an existed-then-deleted (existed_at_truncation=True) absent transcript must be HONORED "
        "as a delete, not recreated"
    )


@pytest.mark.asyncio
async def test_recovery_honors_an_intervening_delete_despite_stale_existed_bit(
    tmp_path,
) -> None:
    """The existed_at_truncation bit is captured once and goes stale: a user can
    delete the session AFTER truncation (on a not-yet-flushed conversation the
    bit even reads existed=False while the truncation save created the file and
    the user then deleted it). Recovery must HONOR the recorded delete and NOT
    recreate the session, whatever the stale bit says — otherwise it resurrects
    a user-deleted session."""
    from pathlib import Path

    from kiro_crew.history import ConversationLog, restore_full_rows_off_loop

    sdir = tmp_path / "sessions"
    sdir.mkdir()
    log = ConversationLog(base_dir=Path(sdir))
    rows = [{"role": "assistant", "content": "DELETED-REPLY", "cls": "msg msg-a"}]

    # The regenerate truncated with existed=False (not-yet-flushed), the file was
    # then created and the user deleted it mid-regenerate — recorded as a delete.
    key = "dashboard:regen-then-delete"
    log.append(key, "user", "hi")  # the conversation was flushed after truncation
    assert log.delete_session(key) is True
    assert not log._path(key).exists()

    # Recovery with the STALE existed_at_truncation=False must NOT recreate the
    # session — the recorded delete wins.
    committed = await restore_full_rows_off_loop(log, key, rows, existed_at_truncation=False)
    assert committed is True
    assert not log._path(key).exists(), (
        "recovery must honor a recorded intervening delete and NOT resurrect a user-deleted "
        "session, even when the stale existed_at_truncation bit reads False"
    )


@pytest.mark.asyncio
async def test_recovery_tombstone_matches_across_alias_spellings(tmp_path) -> None:
    """The delete tombstone must be normalized through the same alias collapsing
    _path relies on: a delete issued under one Slack spelling (slack:<ts>) must
    be honored by a recovery that checks under another (bare <ts>). A raw-string
    tombstone would miss the alias form and resurrect the deleted session."""
    from pathlib import Path

    from kiro_crew.history import ConversationLog, restore_full_rows_off_loop

    sdir = tmp_path / "sessions"
    sdir.mkdir()
    log = ConversationLog(base_dir=Path(sdir))
    rows = [{"role": "assistant", "content": "ALIAS-REPLY", "cls": "msg msg-a"}]

    # Delete under the BARE spelling (stored stems: slack_<ts>, <ts>).
    delete_spelling = "1700000000.123456"
    log.append(delete_spelling, "user", "hi")
    assert log.delete_session(delete_spelling) is True

    # Recovery checks under the COLON spelling (slack:<ts>) — which is NOT one of
    # the literally-stored stems, so only the _path/lock-stem normalization makes
    # it match. A raw-string tombstone would miss it and resurrect the session.
    recovery_spelling = "slack:1700000000.123456"
    assert not log._path(recovery_spelling).exists()
    committed = await restore_full_rows_off_loop(
        log, recovery_spelling, rows, existed_at_truncation=False
    )
    assert committed is True
    assert not log._path(recovery_spelling).exists(), (
        "a delete under one alias spelling must be honored when recovery checks under another — "
        "the tombstone key must be normalized, not a raw string"
    )


def test_pending_recovery_registry_is_bounded(monkeypatch) -> None:
    """_PENDING_RECOVERIES must not grow without limit. A plain entry-count cap
    bounds it deterministically — on overflow the OLDEST entry is dropped (FIFO),
    with no dependency on the durable write (a byte-cap enforced by flushing the
    evicted entry would be circular, since write failure is the only thing that
    fills the registry)."""
    from kiro_crew.dashboard import chat_regenerate as _cr

    _cr._PENDING_RECOVERIES.clear()
    monkeypatch.setattr(_cr, "_PENDING_RECOVERIES_MAX", 4)

    def _entry(n: int):
        return (object(), f"dashboard:conv{n}", [{"role": "assistant", "content": str(n)}], True)

    # Fill well past the cap — even if EVERY write is failing (nothing is popped
    # on commit), the count cap alone bounds the registry.
    for n in range(100):
        _cr._register_pending_recovery(f"dashboard:conv{n}#regen{n}", _entry(n))

    assert len(_cr._PENDING_RECOVERIES) <= 4, (
        f"the registry must be bounded at the count cap even when every write fails "
        f"(size={len(_cr._PENDING_RECOVERIES)})"
    )
    # The newest entries survive; the oldest were dropped (FIFO).
    assert "dashboard:conv99#regen99" in _cr._PENDING_RECOVERIES, "the newest entry must survive"
    assert "dashboard:conv0#regen0" not in _cr._PENDING_RECOVERIES, "the oldest must be dropped"
    # Re-inserting an existing key refreshes in place without growing.
    before = len(_cr._PENDING_RECOVERIES)
    _cr._register_pending_recovery("dashboard:conv99#regen99", _entry(99))
    assert len(_cr._PENDING_RECOVERIES) == before, "re-inserting an existing key must not grow"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_bound_enforcement_does_not_drop_the_restored_reply(
    state, caplog, monkeypatch
) -> None:
    """A near-cap window means the restore splice can be front-trimmed by bound
    enforcement before the save sees it. The reply must NOT be permanently
    dropped: rows evicted by the trim are written durably to the transcript, and
    only surviving rows are broadcast (never a row absent from the committed
    transcript)."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    # The real eager truncation persists the transcript; seed it so the durable
    # retention of trimmed rows targets an existing transcript.
    state.conversation_log.append(original_key, "user", "hi")

    broadcast_rows = []

    def _capture_broadcast(slot_key, row):
        broadcast_rows.append(row.get("content"))

    # Deterministically model bound enforcement front-trimming the just-spliced
    # reply: remove the ORIGINAL-REPLY row (and only it) from the window when the
    # restore runs the bound helper. This is exactly the near-cap + trailing-row
    # scenario, isolated so the test does not depend on the real 10k cap.
    original_enforce = type(slot)._enforce_message_bound

    def _evict_the_restored_reply(self):
        self.messages[:] = [m for m in self.messages if m.get("content") != "ORIGINAL-REPLY"]

    async def _empty_turn(*a, **kw):
        return None

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch.object(state, "_broadcast_chat_message", _capture_broadcast),
        patch.object(type(slot), "_enforce_message_bound", _evict_the_restored_reply),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            await asyncio.sleep(0.05)

    # The trim evicted the reply from the live window — so the ONLY way it can be
    # on disk is the evicted-rows durable write.
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in slot.messages
    ), "precondition: bound enforcement evicted the restored reply from the window"
    # The reply is durably retained on the transcript even though the live window
    # could not hold it.
    rows = state.conversation_log.read_messages(original_key)
    assert any(
        m.get("content") == "ORIGINAL-REPLY" for m in rows
    ), "a bound-enforcement trim must not permanently drop the restored reply"
    # No broadcast announced a row absent from the committed window: the evicted
    # reply is not broadcast (every broadcast row is present in the live slot).
    assert (
        "ORIGINAL-REPLY" not in broadcast_rows
    ), "a restored row trimmed from the committed window must not be broadcast"
    _ = original_enforce  # retained for clarity; patch.object restores it


@pytest.mark.asyncio
async def test_regenerate_survivor_save_does_not_clobber_a_pending_evicted_recovery(
    state, caplog, monkeypatch
) -> None:
    """When bound enforcement evicts restored rows and their durable recovery
    write does NOT commit, a pending entry (holding the evicted rows) is owed to
    a retry/drain under the SHARED restore_expected_key. The survivor merge save
    committing afterwards must NOT clear that entry — unlike the no-eviction case,
    the key holds the evicted-rows recovery, not the already-satisfied pre-turn
    entry, and dropping it would lose the evicted rows. The survivor pop must be
    guarded so this does not happen."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    # Keep the background retry from re-registering the entry before the
    # assertion: a long retry delay means _again sleeps past the check window, so
    # the registry state the test reads is purely the survivor-save pop's own
    # decision, not a retry re-adding what an unguarded pop dropped.
    monkeypatch.setattr(_cr, "_RECOVERY_RETRY_DELAY_SECS", 600.0)

    # Bound enforcement evicts the restored reply from the window.
    def _evict_the_restored_reply(self):
        self.messages[:] = [m for m in self.messages if m.get("content") != "ORIGINAL-REPLY"]

    async def _empty_turn(*a, **kw):
        return None

    # The evicted-rows recovery write (restore_full_rows_off_loop) FAILS, so a
    # pending entry is registered/retained under restore_expected_key. The
    # survivor merge save (save_slot_off_loop) COMMITS, reaching the survivor pop.
    async def _recovery_always_fails(conversation_log, key, rows, **_kw):
        return False

    async def _save_commits(*a, **kw):
        return True

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch.object(type(slot), "_enforce_message_bound", _evict_the_restored_reply),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_always_fails,
        ),
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_save_commits),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            # Let the restore task run its evicted-recovery failure + survivor
            # save + pop. Do NOT await _regenerate_restore_task — by now it is the
            # armed retry (_again), which sleeps 600s; awaiting it would hang. A
            # few loop cycles are enough for the (patched, no-real-IO) restore to
            # reach and run the survivor-save pop.
            for _ in range(10):
                await asyncio.sleep(0)
            # Snapshot the registry state the pop produced, before tearing down.
            entry_present = any(v[1] == original_key for v in _cr._PENDING_RECOVERIES.values())
            # Cancel every background task (incl. the 600s retry) so teardown is
            # clean.
            for _t in list(state._background_tasks):
                _t.cancel()

    # The evicted-rows recovery never committed, so the entry under the original
    # transcript key is RETAINED for the retry/drain — the survivor-save pop must
    # not have clobbered it. (With the retry sleeping 600s, the only thing that
    # could have cleared it is an unguarded pop.)
    assert entry_present, (
        "a committed survivor save must not clear a still-pending evicted-rows "
        "recovery registered under the shared key"
    )
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_recovery_preserves_ts_provenance_and_full_meta(state, caplog) -> None:
    """The recovery write must carry the row's CANONICAL representation: its
    original timestamp, provenance (source_thread/source_user), and FULL meta
    (turn statistics and all) — not a synthesized ts or a reduced {mid} meta."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.messages.append(
        {
            "role": "assistant",
            "content": "REPLY",
            "cls": "msg msg-a",
            "ts": "2020-01-02T03:04:05+00:00",
            "source_thread": "slack-thread-7",
            "source_user": "U-author-9",
            "meta": {"mid": "mid-1", "turn_stats": {"tokens": 123}, "model": "test-model"},
        }
    )
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    async def _truncate_commits(*a, **kw):
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task

    rows = state.conversation_log.read_messages(original_key)
    reply = next((m for m in rows if m.get("content") == "REPLY"), None)
    assert reply is not None, "the reply must be recovered to the original transcript"
    # Original ts preserved — not re-stamped with a fresh monotonic value.
    assert reply.get("ts") == "2020-01-02T03:04:05+00:00", "the original ts must be preserved"
    # Provenance preserved.
    assert reply.get("source_thread") == "slack-thread-7", "source_thread must be preserved"
    assert reply.get("source_user") == "U-author-9", "source_user must be preserved"
    # FULL meta preserved — turn stats and model, not just {mid}.
    meta = reply.get("meta", {})
    assert meta.get("mid") == "mid-1"
    assert meta.get("turn_stats") == {"tokens": 123}, "turn statistics must survive the recovery"
    assert meta.get("model") == "test-model", "meta.model must survive the recovery"


@pytest.mark.asyncio
async def test_regenerate_successful_retry_cleans_the_rebound_slot(state, caplog) -> None:
    """A mid-save rebind whose FIRST recovery write fails, then a retry SUCCEEDS.
    The spliced rows are stripped off the rebound slot immediately on the rebind
    (so no flush can leak them), and the fence clears only once the retry
    confirms the durable write."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()

    saves = []

    async def _truncate_ok_then_rebind_and_refuse(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True  # endpoint truncating write commits
        # The restore's merge write: rebind to a DIFFERENT conversation and
        # refuse (so the restore takes the rebind-rollback branch).
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    writes = {"n": 0}

    async def _recovery_fails_first_then_succeeds(conversation_log, key, rows, **_kw):
        writes["n"] += 1
        if writes["n"] == 1:
            return False  # first durable recovery attempt fails
        for row in rows:
            conversation_log.append_full_message_if_absent(key, row)
        return True

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_refuse,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_fails_first_then_succeeds,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            # Drain the restore task and any re-armed retry task to completion.
            for _ in range(6):
                t = slot._regenerate_restore_task
                if t is None:
                    break
                await t
                await asyncio.sleep(0)

    assert writes["n"] >= 2, "a failed recovery write must be retried"
    # The spliced rows are not on the rebound slot (stripped immediately), so the
    # periodic flush cannot leak the old reply into the new transcript.
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in slot.messages
    ), "the spliced rows must not remain on the rebound slot"
    # And the fence is released only after the retry confirms the write.
    assert slot._regenerate_restore_pending is False, "the fence must clear after a confirmed retry"


@pytest.mark.asyncio
async def test_regenerate_recovery_preserves_chronology_under_concurrent_append(
    state, caplog
) -> None:
    """A newer row (a channel reply / cron result) can land on the original
    transcript AFTER the truncation. The recovered reply, which is OLDER, must
    splice at its chronological position — BEFORE the newer row — not append at
    the tail, or the transcript's order is corrupted."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    # The reply carries an OLD timestamp.
    slot.messages.append(
        {
            "role": "assistant",
            "content": "OLD-REPLY",
            "cls": "msg msg-a",
            "ts": "2020-01-01T00:00:00+00:00",
        }
    )
    slot.drain()
    original_key = slot_history_key(slot)
    # Seed the transcript with the user row, then a NEWER row that landed after
    # the truncation (a concurrent channel reply), so the recovered OLD-REPLY
    # must sort BEFORE it.
    state.conversation_log.append(original_key, "user", "hi")
    state.conversation_log.append(original_key, "assistant", "NEWER-ROW", mid="mid-newer")
    # Force the newer row's ts to be strictly newer than the recovered reply's.
    # (append stamps monotonic now(), which is far newer than 2020, so this holds.)

    async def _truncate_commits(*a, **kw):
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            await asyncio.sleep(0.05)

    rows = state.conversation_log.read_messages(original_key)
    contents = [m.get("content") for m in rows]
    assert "OLD-REPLY" in contents, "the recovered reply must be written to the original transcript"
    assert "NEWER-ROW" in contents, "the concurrently-appended newer row must remain"
    # Chronology preserved: the OLD reply sorts BEFORE the newer row, not appended
    # after it.
    assert contents.index("OLD-REPLY") < contents.index(
        "NEWER-ROW"
    ), "the recovered older reply must splice before the newer row, not append at the tail"


@pytest.mark.asyncio
async def test_regenerate_rebind_strips_rows_before_any_flush(state, caplog) -> None:
    """On a mid-save rebind the spliced recovery rows must be removed from the
    rebound slot BEFORE any periodic flush can run — not left in slot.messages
    pending a retry — so a flush landing between a failed recovery write and the
    next retry cannot persist them into the rebound conversation."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()

    saves = []

    async def _truncate_ok_then_rebind_and_refuse(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    # Recovery ALWAYS fails here: we are testing that even while recovery is
    # still owed (never confirmed), the rows are NOT on the rebound slot.
    async def _recovery_always_fails(conversation_log, key, rows, **_kw):
        return False

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_refuse,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_always_fails,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            # Let the first retry attempt run and fail (it re-arms another).
            t = slot._regenerate_restore_task
            if t is not None:
                await asyncio.sleep(0.3)

    # Even though recovery never confirmed, the rebound slot does NOT hold the
    # old reply — so a periodic flush cannot persist it into the new transcript.
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in slot.messages
    ), "recovery rows must be out of the rebound slot immediately, before any flush"
    # Clean up the pending retry so it does not leak into other tests.
    from kiro_crew.dashboard import chat_regenerate as _cr

    rt = slot._regenerate_restore_task
    if rt is not None:
        rt.cancel()
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_shutdown_drains_pending_recovery(state, caplog) -> None:
    """A recovery write failure then a gateway SHUTDOWN must not lose the reply:
    the shutdown drain awaits an immediate confirmed write for every pending
    recovery, so the original transcript gets the reply even if the background
    retry never ran."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    saves = []

    async def _truncate_ok_then_rebind_and_refuse(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    # The real restore_full_rows_off_loop, but the FIRST call (from the restore)
    # fails; the drain's call (immediate, no backoff) is the real one and
    # succeeds. We simulate the retry never getting to run before shutdown by
    # making the first durable write fail and then draining immediately.
    real_restore = _cr.restore_full_rows_off_loop
    calls = {"n": 0}

    async def _fail_first_then_real(conversation_log, key, rows, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # the restore's first write fails -> a retry is armed
        return await real_restore(conversation_log, key, rows)

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_refuse,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_fail_first_then_real,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            # A retry is now armed/registered. Simulate SHUTDOWN before it runs:
            # cancel the in-flight retry task, then drain.
            rt = slot._regenerate_restore_task
            if rt is not None:
                rt.cancel()
            await _cr.drain_pending_regenerate_recoveries()

    # The shutdown drain wrote the reply to the original transcript despite the
    # background retry being cancelled — the reply is NOT lost.
    rows = state.conversation_log.read_messages(original_key)
    assert any(
        m.get("content") == "ORIGINAL-REPLY" for m in rows
    ), "a shutdown mid-retry must flush the pending recovery, not lose the reply"


@pytest.mark.asyncio
async def test_regenerate_shutdown_drain_retains_a_refused_write(state, caplog) -> None:
    """If the shutdown drain's write returns False (contention/IO error), the
    pending recovery entry must be RETAINED, not dropped — otherwise the reply is
    treated as recovered while it is still deleted on disk (persist-before-publish
    violated)."""
    from kiro_crew.dashboard import chat_regenerate as _cr

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()

    saves = []

    async def _truncate_ok_then_rebind_and_refuse(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    async def _recovery_always_fails(conversation_log, key, rows, **_kw):
        return False

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_refuse,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_always_fails,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            # A failed recovery registered a pending entry. Cancel the retry task
            # so it does not race the drain, then drain (still failing).
            rt = slot._regenerate_restore_task
            if rt is not None:
                rt.cancel()
            assert (
                len(_cr._PENDING_RECOVERIES) >= 1
            ), "a failed recovery must register a pending entry"
            await _cr.drain_pending_regenerate_recoveries()

    # The write kept failing, so the entry is RETAINED (not dropped) — the reply
    # is never treated as recovered while still deleted on disk.
    assert (
        len(_cr._PENDING_RECOVERIES) >= 1
    ), "a refused shutdown-drain write must retain the pending entry, not drop it"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_first_recovery_write_is_registered_before_it_is_awaited(
    state, caplog
) -> None:
    """The FIRST recovery write must be registered in the shutdown-drain registry
    BEFORE it is awaited, not only after it fails. A gateway stop landing during
    that first write (while the coroutine is suspended on disk I/O, with the rows
    already held out of the slot window) must still find the write in the
    registry so the drain can finish it — otherwise the reply is lost. This
    asserts the entry is already present at the moment the first write runs."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    saves = []

    async def _truncate_ok_then_rebind_and_refuse(*a, **kw):
        saves.append(kw)
        if len(saves) == 1:
            return True
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    real_restore = _cr.restore_full_rows_off_loop
    seen = {"registered_during_first_write": None}

    async def _observe_registry_then_real(conversation_log, key, rows, **_kw):
        # At the moment the FIRST recovery write runs, the entry must already be
        # in the registry (register-BEFORE-write). The pre-fix code registered
        # only after this call returned False, so this would be empty on the
        # first attempt and the reply would be invisible to a drain landing here.
        if seen["registered_during_first_write"] is None:
            seen["registered_during_first_write"] = any(
                v[1] == key for v in _cr._PENDING_RECOVERIES.values()
            )
        return await real_restore(conversation_log, key, rows)

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_refuse,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_observe_registry_then_real,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            rt = slot._regenerate_restore_task
            if rt is not None:
                try:
                    await rt
                except Exception:
                    pass

    assert seen["registered_during_first_write"] is True, (
        "the first recovery write must be registered in the shutdown-drain "
        "registry BEFORE it is awaited, so a stop mid-first-write is recoverable"
    )
    # The write ultimately succeeded, so the entry is cleaned up.
    rows = state.conversation_log.read_messages(original_key)
    assert any(m.get("content") == "ORIGINAL-REPLY" for m in rows)
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_shutdown_during_the_turn_recovers_the_truncated_reply(
    state, caplog
) -> None:
    """A gateway stop DURING the regenerate turn — after the eager truncation has
    persisted the original transcript short of its reply, but before the turn
    ends and the done-callback runs — must not lose the reply. The removed reply
    is registered as a pending recovery synchronously at truncation (not only at
    turn-end), so the shutdown drain can flush it back to the original transcript
    even though the turn never completed. In-flight regenerate + a stop are both
    ordinary, so this window must be covered."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    # The turn BLOCKS, modelling a gateway stop landing mid-turn: it never
    # produces a reply and the done-callback never runs before we drain.
    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _blocking_turn(*a, **kw):
        turn_running.set()
        await release_turn.wait()
        return None

    with (patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_blocking_turn),):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            # Wait until the turn is in-flight (truncation already persisted, the
            # pre-turn recovery registered), then drain as if the gateway stopped
            # here — the turn has NOT produced a reply and its done-callback has
            # NOT run. The drain QUIESCES the in-flight turn (cancels and awaits
            # it) so the turn's own write path settles before the drain writes,
            # rather than both writing the row.
            await asyncio.wait_for(turn_running.wait(), timeout=5)
            assert any(
                v[1] == original_key for v in _cr._PENDING_RECOVERIES.values()
            ), "the removed reply must be registered as a pending recovery at truncation time"
            await _cr.drain_pending_regenerate_recoveries()
            # The drain quiesced the turn; releasing the gate now is harmless.
            release_turn.set()
            if slot.task is not None:
                try:
                    await slot.task
                except (Exception, asyncio.CancelledError):
                    pass
            rt = slot._regenerate_restore_task
            if rt is not None:
                try:
                    await rt
                except (Exception, asyncio.CancelledError):
                    pass

    # The removed reply is recovered to the original transcript despite the turn
    # never producing one — and recovered EXACTLY ONCE (the quiesce makes the
    # drain and the turn's own write path cooperate rather than both writing).
    rows = state.conversation_log.read_messages(original_key)
    original_copies = [m for m in rows if m.get("content") == "ORIGINAL-REPLY"]
    assert (
        len(original_copies) == 1
    ), "a shutdown during the turn must recover the truncated reply exactly once, not duplicate it"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_partial_reply_attach_failure_clears_the_pending_entry(
    state, caplog
) -> None:
    """An abnormally-ended regenerate (a partial reply persisted outside
    _flush_segment, so _pending_variants stays set) whose variant-attach save
    does NOT commit must not leave a stale _PENDING_RECOVERIES entry. If it did,
    a later shutdown drain would write the old reply as a TOP-LEVEL row —
    duplicating it against the copy the dirty flush would persist as a nested
    variant (the top-level write is not deduped against a nested variant). The
    non-committed branch reverts the attach and recovers the old reply via the
    single drain-registry path, which clears the entry on a confirmed write — so
    no stale entry lingers and the reply lands exactly once."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    # A turn that produces a PARTIAL reply: append an assistant row with a mid
    # and record it in _turn_reply_mids_all, WITHOUT going through _flush_segment
    # (so _pending_variants stays set). This drives _restore_previous_reply down
    # the partial-reply variant-attach path.
    async def _partial_reply_turn(*a, **kw):
        partial = {"role": "assistant", "content": "PARTIAL-REPLY", "meta": {"mid": "mid-partial"}}
        slot.messages.append(partial)
        if not isinstance(getattr(slot, "_turn_reply_mids_all", None), list):
            slot._turn_reply_mids_all = []
        slot._turn_reply_mids_all.append("mid-partial")
        return None

    real_restore = _cr.restore_full_rows_off_loop
    saves = {"n": 0}

    # The eager truncation save commits; the LATER variant-attach save (same
    # transcript) does NOT commit, driving the non-committed branch.
    async def _truncate_ok_attach_fails(*a, **kw):
        saves["n"] += 1
        return saves["n"] == 1

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_partial_reply_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_attach_fails,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            rt = slot._regenerate_restore_task
            if rt is not None:
                try:
                    await rt
                except Exception:
                    pass

    # The non-committed attach recovered the old reply to the original transcript
    # (via the real restore_full_rows_off_loop), so the pending entry is cleared —
    # no stale entry for a later drain to double-write.
    assert not any(v[1] == original_key for v in _cr._PENDING_RECOVERIES.values()), (
        "a non-committed partial-reply attach must clear its pending recovery "
        "entry once the old reply is durably recovered"
    )
    # The old reply is on the original transcript exactly once (no duplicate from
    # a stale-entry drain), as a top-level row.
    rows = state.conversation_log.read_messages(original_key)
    old_reply_rows = [m for m in rows if m.get("content") == "ORIGINAL-REPLY"]
    assert len(old_reply_rows) == 1, (
        "the recovered old reply must appear exactly once on the original "
        f"transcript, not duplicated (found {len(old_reply_rows)})"
    )
    # Draining now must NOT add a second copy (nothing stale left to flush).
    await _cr.drain_pending_regenerate_recoveries()
    rows_after = state.conversation_log.read_messages(original_key)
    assert (
        len([m for m in rows_after if m.get("content") == "ORIGINAL-REPLY"]) == 1
    ), "a drain after a cleared entry must not duplicate the recovered reply"
    _cr._PENDING_RECOVERIES.clear()
    _ = real_restore


@pytest.mark.asyncio
async def test_regenerate_reply_landed_retains_entry_until_replacement_save_commits(
    state, caplog
) -> None:
    """On the reply-landed (success) path, the pre-turn recovery entry must be
    dropped only AFTER a confirmed replacement save — not synchronously in the
    done-callback. _flush_segment attaches the old reply as a variant of the new
    reply in the LIVE window only (no durable write of its own), so a transient
    replacement-save failure plus a correlated gateway stop would lose both
    replies from an already-truncated transcript if the entry were already gone.
    A non-committing confirm save must RETAIN the entry for the drain."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    # A reply-landed turn: emulate _flush_segment consuming the stashed variants
    # (attach the old reply to a NEW reply row, clear _pending_variants) so the
    # done-callback takes the `not slot._pending_variants` branch.
    async def _reply_landed_turn(*a, **kw):
        new_reply = {"role": "assistant", "content": "NEW-REPLY", "meta": {"mid": "mid-new"}}
        if slot._pending_variants:
            new_reply["variants"] = list(slot._pending_variants)
            new_reply["variant_idx"] = len(new_reply["variants"]) - 1
            slot._pending_variants = []
            slot._regenerate_restore_pending = False
        slot.messages.append(new_reply)
        return None

    saves = {"n": 0}

    # Truncation save (call 1) commits; the confirming replacement save (call 2)
    # does NOT commit — the entry must be retained.
    async def _truncate_ok_confirm_fails(*a, **kw):
        saves["n"] += 1
        return saves["n"] == 1

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_reply_landed_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_confirm_fails,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            # Let the confirm-then-clear task run.
            for _ in range(5):
                await asyncio.sleep(0)

    # The confirming replacement save did not commit, so the pre-turn recovery
    # entry is RETAINED — a shutdown drain can still write the old reply back.
    assert any(v[1] == original_key for v in _cr._PENDING_RECOVERIES.values()), (
        "a non-committing replacement save on the reply-landed path must retain "
        "the pending recovery entry for the drain, not drop it synchronously"
    )
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_second_regenerate_does_not_clobber_the_first_pending_recovery(
    state, caplog, monkeypatch
) -> None:
    """Two regenerates on the SAME transcript: the first's recovery write is
    still pending (uncommitted) when the second runs. The second's pre-turn
    registration must NOT overwrite the first's entry — both are owed a durable
    write to the same transcript, so a per-regenerate entry key keeps them
    distinct. Overwriting (the old transcript-keyed registration) would silently
    lose the first reply on a stop."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()
    # No real retry during the test window; we only need the pending entries.
    monkeypatch.setattr(_cr, "_RECOVERY_RETRY_DELAY_SECS", 600.0)

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "REPLY-ONE")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    async def _truncate_commits(*a, **kw):
        return True

    # The turn rebinds so the restore recovers to the ORIGINAL transcript (the
    # durable-recovery path that registers a pending entry), and the recovery
    # write FAILS so the entry stays pending.
    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    async def _recovery_always_fails(conversation_log, key, rows, **_kw):
        return False

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_recovery_always_fails,
        ),
    ):
        async with _client(state) as client:
            # First regenerate: leaves a pending entry (recovery write failed),
            # then its fence releases.
            resp1 = await client.post("/api/chat/slots/s1/regenerate")
            assert resp1.status == 200
            if slot.task is not None:
                await slot.task
            for _ in range(10):
                await asyncio.sleep(0)
            # Release the fence so the second regenerate is admitted (model the
            # bounded-retry / reply-landed fence release), and restore the slot's
            # routing to the original transcript for the second regenerate.
            slot._regenerate_restore_pending = False
            slot._regenerate_restore_task = None
            slot.linked_session_key = None
            first_pending = sum(1 for v in _cr._PENDING_RECOVERIES.values() if v[1] == original_key)
            assert first_pending >= 1, "precondition: the first regenerate left a pending entry"

            # Put a fresh reply on the slot so the second regenerate has
            # something to truncate.
            slot.append("assistant", "REPLY-TWO")
            slot.drain()

            # Second regenerate on the SAME transcript.
            resp2 = await client.post("/api/chat/slots/s1/regenerate")
            assert resp2.status == 200
            if slot.task is not None:
                await slot.task
            for _ in range(10):
                await asyncio.sleep(0)
            second_pending = sum(
                1 for v in _cr._PENDING_RECOVERIES.values() if v[1] == original_key
            )
            # Cancel background tasks (600s retries) for clean teardown.
            for _t in list(state._background_tasks):
                _t.cancel()

    # Both regenerates' pending entries survive — the second did NOT overwrite
    # the first's still-pending entry.
    assert second_pending >= 2, (
        "a second regenerate on the same transcript must not clobber the first's "
        f"still-pending recovery entry (pending entries for the transcript={second_pending})"
    )
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_recovery_does_not_persist_transient_rows(state, caplog) -> None:
    """A transient control row (a `done`/`chunk`) trailing the reply in the
    removed rows must NOT be written to the transcript by recovery — only
    canonical message entries are persisted."""
    from kiro_crew.dashboard.chat_utils import slot_history_key

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    # A transient control row right after the reply — _build_message_entry
    # returns None for these, so recovery must drop it.
    slot.messages.append({"role": "done", "content": "TRANSIENT-DONE"})
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    async def _truncate_commits(*a, **kw):
        return True

    async def _empty_turn_then_rebind(*a, **kw):
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_truncate_commits),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task
            await asyncio.sleep(0.05)

    rows = state.conversation_log.read_messages(original_key)
    contents = [m.get("content") for m in rows]
    roles = [m.get("role") for m in rows]
    assert "ORIGINAL-REPLY" in contents, "the real reply must be recovered"
    assert "TRANSIENT-DONE" not in contents, "a transient control row must not be persisted"
    assert "done" not in roles, "no transient `done` row may land in the transcript"


def test_append_full_message_distinguishes_idless_duplicates_by_ts(tmp_path) -> None:
    """Two content-identical legacy replies (no meta.mid) with DIFFERENT
    timestamps are distinct occurrences: append_full_message_if_absent must NOT
    dedup the second against the first by content alone — so recovery can re-add
    the specific removed one."""
    from pathlib import Path

    from kiro_crew.history import ConversationLog

    monkey_dir = tmp_path / "sessions"
    monkey_dir.mkdir()
    log = ConversationLog(base_dir=Path(monkey_dir))
    key = "dashboard:dup"

    # Two id-less assistant rows with identical content but different ts.
    row_a = {
        "role": "assistant",
        "content": "SAME-TEXT",
        "cls": "msg msg-a",
        "ts": "2020-01-01T00:00:00+00:00",
    }
    row_b = {
        "role": "assistant",
        "content": "SAME-TEXT",
        "cls": "msg msg-a",
        "ts": "2020-01-02T00:00:00+00:00",
    }
    assert log.append_full_message_if_absent(key, row_a) is True
    # The SECOND, ts-distinct occurrence must be written (not deduped away).
    assert (
        log.append_full_message_if_absent(key, row_b) is True
    ), "a content-identical legacy reply with a different ts must not be deduped away"
    # Re-adding the EXACT same row (same content AND ts) is correctly a no-op.
    assert log.append_full_message_if_absent(key, row_a) is False
    rows = log.read_messages(key)
    same = [m for m in rows if m.get("content") == "SAME-TEXT"]
    assert len(same) == 2, "both ts-distinct occurrences must be present on disk"


def test_append_full_message_dedups_against_a_nested_variant(tmp_path) -> None:
    """A row already present as a nested `variants` entry of an on-disk row must
    be treated as already persisted (return False) — a regenerate attaches the
    previous reply as a VARIANT of the new reply, so the drain writing that
    previous reply as a TOP-LEVEL row would duplicate it. The variant-chain
    dedup prevents the double-write."""
    from pathlib import Path

    from kiro_crew.history import ConversationLog

    base = tmp_path / "sessions"
    base.mkdir()
    log = ConversationLog(base_dir=Path(base))
    key = "dashboard:variant-dedup"

    # On disk: a NEW reply whose variant chain holds the OLD reply (as a
    # regenerate's _flush_segment would persist it).
    log.append(key, "user", "hi")
    new_reply = {
        "role": "assistant",
        "content": "NEW-REPLY",
        "ts": "2020-01-02T00:00:00+00:00",
        "meta": {"mid": "mid-new"},
        "variants": [
            {"content": "OLD-REPLY", "ts": "2020-01-01T00:00:00+00:00"},
            {"content": "NEW-REPLY", "ts": "2020-01-02T00:00:00+00:00"},
        ],
    }
    # Write the new reply row with its variant chain directly.
    assert log.append_full_message_if_absent(key, new_reply) is True

    # The drain tries to write the OLD reply as a TOP-LEVEL row (its original
    # mid-bearing form). It is already present as a variant of the new reply, so
    # the write must be deduped (return False) — no top-level duplicate.
    old_reply_top_level = {
        "role": "assistant",
        "content": "OLD-REPLY",
        "ts": "2020-01-01T00:00:00+00:00",
        "meta": {"mid": "mid-old"},
    }
    assert log.append_full_message_if_absent(key, old_reply_top_level) is False, (
        "a reply already present as a nested variant must be deduped against the "
        "drain's top-level write, not duplicated"
    )
    rows = log.read_messages(key)
    top_level_old = [m for m in rows if m.get("content") == "OLD-REPLY"]
    assert (
        len(top_level_old) == 0
    ), "the old reply must NOT appear as a top-level row (it is a variant)"


def test_recovery_persists_a_variant_inline_image(tmp_path) -> None:
    """A variant carrying an agent-produced inline image, recovered via the
    rebound-slot path, must have its image bytes copied into the durable store
    and its reference rewritten — exactly as the primary content and the slot
    save's own variant loop do. Without that, the recovered variant keeps a
    reference to the agent scratch dir, whose bytes are reclaimed when the agent
    process dies, and this path has no live window to repair it later."""
    from pathlib import Path

    from kiro_crew.chat_attachments import attachments_dir
    from kiro_crew.history import ConversationLog

    # A one-pixel PNG with real magic bytes, in a scratch dir OUTSIDE the store.
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    scratch = tmp_path / "agent-scratch"
    scratch.mkdir()
    png = scratch / "shot.png"
    png.write_bytes(png_bytes)

    base = tmp_path / "sessions"
    base.mkdir()
    log = ConversationLog(base_dir=Path(base))
    key = "dashboard:variant-img"

    # An assistant reply whose VARIANT (an alternate reply the user can switch
    # back to) carries the inline image pointing at the agent scratch file.
    scratch_ref = f"![shot]({png})"
    row = {
        "role": "assistant",
        "content": "primary reply, no image",
        "ts": "2020-01-01T00:00:00+00:00",
        "variants": [
            {"role": "assistant", "content": "chosen variant, no image"},
            {"role": "assistant", "content": f"older variant {scratch_ref}"},
        ],
    }

    assert log.append_full_message_if_absent(key, row) is True

    rows = log.read_messages(key)
    assistant = [m for m in rows if m.get("role") == "assistant"]
    assert assistant, "the recovered reply must be on disk"
    variants = assistant[0].get("variants") or []
    img_variant = next((v for v in variants if "![shot]" in v.get("content", "")), None)
    assert img_variant is not None, "the image-bearing variant must survive recovery"

    # The reference must point into the transcript's durable attachments dir,
    # not the scratch dir, and the bytes must be copied there.
    store = attachments_dir(base, log._path(key).stem)
    assert str(scratch) not in img_variant["content"], (
        "the variant image must NOT keep its agent-scratch reference — "
        "those bytes are reclaimed when the agent dies"
    )
    assert (
        str(store) in img_variant["content"]
    ), "the variant image reference must be rewritten into the durable store"
    copied = list(store.glob("*shot.png"))
    assert copied, "the variant's image bytes must be copied into the durable store"
    assert copied[0].read_bytes() == png_bytes, "the copied image must be byte-identical"


@pytest.mark.asyncio
async def test_unconsumed_variants_restore_the_previous_reply(state, caplog) -> None:
    """If the flush never picks the stash up, the turn produced no reply, so the
    done-callback restores the previous reply and clears the stash rather than
    leaking it into the next turn or losing the reply."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()
    ):  # returns without consuming _pending_variants
        with caplog.at_level("INFO"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                if slot.task is not None:
                    await slot.task
                await asyncio.sleep(0)
                if slot._regenerate_restore_task is not None:
                    await slot._regenerate_restore_task

    assert slot._pending_variants == []
    assert [m["role"] for m in slot.messages] == ["user", "assistant"]
    assert slot.messages[-1]["content"] == "hello v1"
    assert "restored the previous reply" in caplog.text


@pytest.mark.asyncio
async def test_regenerate_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 409
    finally:
        slot.task.cancel()


# ── switch-variant ──


@pytest.mark.asyncio
async def test_switch_variant_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/nope/switch-variant", json={"index": 0})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_json_body(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/switch-variant",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_object_body(state) -> None:
    """A JSON array has no .get(), so an unguarded handler would 500."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json=[0])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_integer_index(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for body in ({"index": "second"}, {}):
            resp = await client.post("/api/chat/slots/s1/switch-variant", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid index"


@pytest.mark.asyncio
async def test_switch_variant_needs_an_assistant_row_with_variants(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "only one answer")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        assert (await resp.json())["error"] == "no variants"


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_corrupt_variant_entry(state) -> None:
    """A restored transcript can hold a non-dict entry; picking it would 500."""
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v1")
    slot.messages[-1]["variants"] = ["a bare string, not an entry"]
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        assert (await resp.json())["error"] == "corrupt variant entry"


@pytest.mark.asyncio
async def test_switch_variant_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v1")
    slot.messages[-1]["variants"] = [{"content": "v1"}]
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 409
    finally:
        slot.task.cancel()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("regenerate", None),
        ("switch-variant", {"index": 0}),
        ("edit-resend", {"index": 0, "content": "edited"}),
    ],
)
@pytest.mark.asyncio
async def test_destructive_history_endpoints_refuse_a_paused_boundary(state, path, body) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "original")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [
        {"content": "v1", "ts": "t1"},
        {"content": "v2", "ts": "t2"},
    ]
    slot.stage_boundary.arm(1, consumed=True)
    assert slot.running is True and slot.turn_running is False

    async with _client(state) as client:
        response = await client.post(f"/api/chat/slots/s1/{path}", json=body)
        payload = await response.json()

    assert response.status == 409
    assert payload == {"error": "slot is busy", "code": "slot_busy"}
    assert [message["content"] for message in slot.messages] == ["original", "v2"]


@pytest.mark.asyncio
async def test_switch_variant_broadcasts_redacted_content(state) -> None:
    """The broadcast leaves the process, so the chosen variant is redacted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "what is the key?")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [
        {"content": "the key is AKIAIOSFODNN7EXAMPLE", "ts": "t1"},
        {"content": "v2", "ts": "t2"},
    ]
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 200
        assert (await resp.json())["index"] == 0

    msg_type, payload = state.broadcast_ws.call_args.args
    assert msg_type == "chat_variant_switch"
    assert payload["index"] == 0
    assert "AKIAIOSFODNN7EXAMPLE" not in payload["content"]
    # The stored row keeps the real content; only the wire copy is redacted.
    assert slot.messages[-1]["content"] == "the key is AKIAIOSFODNN7EXAMPLE"
    assert slot.messages[-1]["ts"] == "t1"


@pytest.mark.asyncio
async def test_switch_variant_survives_a_persist_failure(state, caplog) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2"}]

    with patch(
        "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
        new=AsyncMock(side_effect=OSError("disk full")),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})

    assert resp.status == 200
    assert "switch-variant: failed to persist" in caplog.text
    assert slot.messages[-1]["content"] == "v1"


# ── recreate-race slot-identity guard ──
#
# Both truncating saves run under slot._lock but dispatch the write to a worker
# thread; the event loop is free across that one await, and a same-name
# close-and-recreate is NOT serialized against this lock (the cleanup pops
# state._slots[name] and get_or_create_slot re-inserts, neither taking the
# original lock). The rewrite resolves its target file from the slot object it
# was handed, so an unguarded write lands the truncation on the replacement's
# transcript. The fix carries the same PAIR edit-resend carries:
#   * expected_history_key = slot_history_key(slot), captured before the await,
#     so the save refuses when the routing resolves to a different key -- a
#     RENAMED replacement;
#   * a state._slots[name] object-identity check immediately before the write,
#     so a SAME-NAME recreate (which keeps the key identical, waving the routing
#     check through) skips the write instead.
# These tests assert the pin reaches the write, and that a same-name swap before
# the write suppresses it. The disk-side routing refusal itself is covered by
# _save_slot_to_history's own expected_history_key tests.


@pytest.mark.asyncio
async def test_regenerate_pins_the_truncating_write_to_its_transcript(state) -> None:
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "hi")
    slot.append("assistant", "keep-me")
    slot.drain()

    saved = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=saved),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            if slot._regenerate_restore_task is not None:
                await slot._regenerate_restore_task

    # Both writes go through save_slot_off_loop: the endpoint's truncating write
    # and, since the stubbed turn produces no reply, the restore's ordinary-merge
    # write. Both carry the slot's own transcript + map key as authorization pins
    # (the restore uses the key captured before the awaits, not one re-resolved
    # inside the coroutine).
    assert saved.await_count == 2
    truncating_write = saved.await_args_list[0]
    assert truncating_write.kwargs["expected_history_key"] == "orig:key"
    assert truncating_write.kwargs["expected_slot_name"] == "s1"
    restore_write = saved.await_args_list[1]
    assert restore_write.kwargs["expected_history_key"] == "orig:key"
    assert restore_write.kwargs["expected_slot_name"] == "s1"
    # The restore write is durable (best_effort=False) and takes the ordinary
    # merge path — no explicit messages snapshot (which would force the
    # collect_foreign-off rewrite).
    assert restore_write.kwargs.get("best_effort") is False
    assert restore_write.kwargs.get("messages") is None
    assert (len(restore_write.args) < 3) or (restore_write.args[2] is None)


@pytest.mark.asyncio
async def test_regenerate_skips_the_write_when_the_slot_is_recreated(state) -> None:
    """A same-name recreate that lands INSIDE the executor wait -- after the
    caller dispatches the write, while the worker thread holds the lock --
    keeps the history key identical, so only an object-identity recheck at the
    locked commit boundary catches it. The truncating write must not commit
    onto the replacement's transcript."""
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "hi")
    slot.append("assistant", "keep-me-1")
    slot.append("user", "again")
    slot.append("assistant", "keep-me-2")
    slot.drain()
    # A replacement bound to the SAME transcript key -- what a recreate that
    # resumes the same session produces, so the routing check alone waves it
    # through.
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")

    real_status = state.conversation_log.get_metadata_status

    def _swap_inside_the_locked_write(key):
        # get_metadata_status runs inside the save's _locked region, before the
        # identity recheck -- model the recreate landing in that window.
        if state._slots.get("s1") is slot:
            state._slots["s1"] = replacement
        return real_status(key)

    with (
        patch.object(
            state.conversation_log,
            "get_metadata_status",
            side_effect=_swap_inside_the_locked_write,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 409
            assert (await resp.json())["code"] == "regenerate_save_refused"
            await asyncio.sleep(0)

    # The truncation never reached disk for the transcript the replacement holds.
    assert state.conversation_log.get_metadata("orig:key") == {}
    # And the turn was not dispatched onto the removed slot.
    assert run.await_count == 0


@pytest.mark.asyncio
async def test_switch_variant_pins_the_persist_to_its_transcript(state) -> None:
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2", "ts": "t2"}]
    slot.drain()

    saved = AsyncMock(return_value=True)
    with patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=saved):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200

    assert saved.await_count == 1
    assert saved.await_args.kwargs["expected_history_key"] == "orig:key"
    assert saved.await_args.kwargs["expected_slot_name"] == "s1"


@pytest.mark.asyncio
async def test_switch_variant_skips_the_write_when_the_slot_is_recreated(state) -> None:
    """Switch-variant carries the same object-identity recheck at the save's
    locked commit boundary: a same-name recreate landing inside the executor
    wait suppresses the persist onto the replacement's transcript."""
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2", "ts": "t2"}]
    slot.drain()
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")

    real_status = state.conversation_log.get_metadata_status

    def _swap_inside_the_locked_write(key):
        if state._slots.get("s1") is slot:
            state._slots["s1"] = replacement
        return real_status(key)

    with patch.object(
        state.conversation_log,
        "get_metadata_status",
        side_effect=_swap_inside_the_locked_write,
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 409
            assert (await resp.json())["code"] == "switch_variant_save_refused"

    assert state.conversation_log.get_metadata("orig:key") == {}
    # A refused persist must not announce a switch no transcript holds.
    assert not any(
        call.args and call.args[0] == "chat_variant_switch"
        for call in state.broadcast_ws.call_args_list
    )


# ── edit-resend ──


@pytest.mark.asyncio
async def test_edit_resend_pins_the_truncating_write_to_its_transcript(state) -> None:
    """Edit-resend carries BOTH axes into the write, like its two siblings.

    Its loop-side checks run before the save is dispatched, and the event loop
    is free from there until the worker commits, so neither of them decides the
    commit. ``expected_history_key`` alone leaves the same-name case open: a
    recreate resuming the same transcript keeps the key identical.
    """
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "deploy alpha", ts="t1")
    slot.append("assistant", "deployed alpha", ts="t2")
    slot.drain()

    saved = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=saved),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert saved.await_count == 1
    assert saved.await_args.kwargs["expected_history_key"] == "orig:key"
    assert saved.await_args.kwargs["expected_slot_name"] == "s1"


@pytest.mark.asyncio
async def test_edit_resend_skips_the_write_when_the_slot_is_recreated(state) -> None:
    """A same-name recreate landing inside the locked write suppresses the rewrite.

    The replacement is bound to the SAME transcript key, which is what a recreate
    resuming the same session produces, so the routing pin waves it through and
    only the object-identity recheck at the commit boundary refuses. The
    truncated window must not reach the transcript the replacement now holds, and
    the edited prompt must not be dispatched onto the slot being torn down.
    """
    slot = state.get_or_create_slot("s1", linked_session_key="orig:key")
    slot.append("user", "keep-me-1", ts="t1")
    slot.append("assistant", "keep-me-2", ts="t2")
    slot.append("user", "keep-me-3", ts="t3")
    slot.append("assistant", "keep-me-4", ts="t4")
    slot.drain()
    replacement = state.get_or_create_slot("s1b", linked_session_key="orig:key")

    real_status = state.conversation_log.get_metadata_status

    def _swap_inside_the_locked_write(key):
        # Runs inside the save's ``_locked`` region, before the identity
        # recheck -- the window a recreate lands in.
        if state._slots.get("s1") is slot:
            state._slots["s1"] = replacement
        return real_status(key)

    with (
        patch.object(
            state.conversation_log,
            "get_metadata_status",
            side_effect=_swap_inside_the_locked_write,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_save_failed"
            await asyncio.sleep(0)

    assert state.conversation_log.get_metadata("orig:key") == {}
    assert run.await_count == 0
    # Nothing was mutated on the live slot either: the refusal happens before
    # the commit, so the original window is intact for a retry.
    assert [m["content"] for m in slot.messages] == [
        "keep-me-1",
        "keep-me-2",
        "keep-me-3",
        "keep-me-4",
    ]


@pytest.mark.asyncio
async def test_edit_resend_by_ts_truncates_and_resends(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "deploy alpha", ts="t1")
    slot.append("assistant", "deployed alpha", ts="t2")
    slot.append("user", "deploy beta", ts="t3")
    slot.append("assistant", "deployed beta", ts="t4")
    slot.drain()
    run = AsyncMock()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"ts": "t3", "content": "  deploy gamma  "},
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot.messages] == [
        "deploy alpha",
        "deployed alpha",
        "deploy gamma",
    ]
    assert run.await_args.args[2] == "deploy gamma"
    assert state.push_slots_update.called


@pytest.mark.asyncio
async def test_edit_resend_by_index_truncates_from_that_row(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot.messages] == ["edited"]


@pytest.mark.asyncio
async def test_edit_resend_delivers_the_owners_edit_as_typed(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "use AKIAIOSFODNN7EXAMPLE please"},
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # No request app, so the edit is the session owner's own words: it is
    # delivered as typed into both the persisted row and the turn input, the
    # same rule an idle send and a steer follow. An app-driven edit still
    # redacts (test_queued_user_text_display covers that boundary).
    assert slot.messages[-1]["content"] == "use AKIAIOSFODNN7EXAMPLE please"
    assert run.await_args.args[2] == "use AKIAIOSFODNN7EXAMPLE please"


@pytest.mark.asyncio
async def test_edit_resend_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/nope/edit-resend", json={"index": 0, "content": "x"}
        )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_json_body(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_object_body(state) -> None:
    """A valid-JSON array has no .get() -- without the guard this is a 500."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/edit-resend", json=["x"])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_edit_resend_requires_non_blank_content(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for body in ({"index": 0, "content": "   "}, {"index": 0}):
            resp = await client.post("/api/chat/slots/s1/edit-resend", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "content is required"


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_string_content(state) -> None:
    """A PRESENT non-string ``content`` has no ``.strip()``.

    Without the type check this is an ``AttributeError`` -> 500 on a body a
    caller can trivially send, so the failure is unreadable rather than a 400
    naming the field. ``None`` stays out of it: an empty composer sends that and
    must keep answering ``content_required``.
    """
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for bad in (123, True, {"text": "x"}, ["x"]):
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": bad}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_content"
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": None}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "content_required"


@pytest.mark.asyncio
async def test_edit_resend_rejects_an_oversize_content(state) -> None:
    """The cap matches the sibling ``rewind``/``fork`` boundaries.

    One edit of the same message must not be accepted by one endpoint and
    refused by another, and the refusal must land BEFORE the destructive
    boundary rather than after the native conversation is already discarded.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend",
            json={"index": 0, "content": "x" * (chat_regenerate._MAX_EDIT_CONTENT_CHARS + 1)},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "content_too_long"
    # The refusal is pre-boundary: nothing was discarded and the window stands.
    state.sessions.discard_conversation.assert_not_awaited()
    assert [m["content"] for m in slot.messages] == ["first"]


@pytest.mark.asyncio
async def test_edit_resend_unknown_ts_is_rejected(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first", ts="t1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"ts": "t9", "content": "edited"}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "user message not found for ts"
    assert len(slot.messages) == 1


@pytest.mark.asyncio
async def test_edit_resend_index_must_point_at_a_user_row(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 1, "content": "edited"}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "index is not a user message"


@pytest.mark.asyncio
async def test_edit_resend_needs_an_index_or_a_ts(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    async with _client(state) as client:
        for body in ({"content": "edited"}, {"index": 99, "content": "edited"}):
            resp = await client.post("/api/chat/slots/s1/edit-resend", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "index or ts required"


@pytest.mark.asyncio
async def test_edit_resend_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
        assert resp.status == 409
        assert [m["content"] for m in slot.messages] == ["first"]
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_edit_resend_readiness_latch_blocks_before_the_truncation(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    blocked = web.json_response({"error": "kiro not verified"}, status=503)

    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=blocked),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )

    assert resp.status == 503
    assert [m["content"] for m in slot.messages] == ["first"]


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_history_save_raises(state, caplog) -> None:
    """A failed rewrite is now a retryable 503 (was log-and-continue 200): the
    live slot is untouched and no replacement turn is dispatched from state that
    was never persisted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            side_effect=OSError("disk full"),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
                assert resp.status == 503
                assert (await resp.json())["code"] == "edit_resend_save_failed"
                await asyncio.sleep(0)

    assert "edit-resend: failed to persist" in caplog.text
    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_save_is_refused(state) -> None:
    """A save refused by its own guards (returns False) must 503, not dispatch:
    the session was deleted or the slot rebound while the write awaited its
    lock, so nothing was persisted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=False),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_save_failed"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_native_boundary_cannot_be_discarded(state) -> None:
    """A failed discard leaves the original branch in place with a retryable
    503 -- no history rewrite, no replacement turn."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))
    run = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history") as save,
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_prepare_failed"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    save.assert_not_called()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_sid_flush_fails(state) -> None:
    """The cleared resume sid must be durable before the commit: a flush failure
    takes the same 503 prepare-failed path as a failed discard."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    run = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history") as save,
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_prepare_failed"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    state.sessions.aflush.assert_awaited_once()
    save.assert_not_called()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_a_busy_session_with_409(state) -> None:
    """A busy native session (discard returns False, an inbound channel reply in
    flight) must 409 with the slot untouched and no flush/save/dispatch."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.discard_conversation = AsyncMock(return_value=False)
    run = AsyncMock()

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history") as save,
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "edit_resend_session_busy"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    state.sessions.aflush.assert_not_awaited()
    save.assert_not_called()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_discards_the_native_conversation_before_persisting(state) -> None:
    """The happy path clears the native conversation (once, skip_if_busy) BEFORE
    the history save, and dispatches the edited turn only after both boundaries
    commit."""
    order: list[str] = []
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        order.append(f"discard:{key}:{kwargs.get('skip_if_busy')}")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    def _save(*_args, **_kwargs):
        order.append("save")
        return True

    run = AsyncMock()
    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history", side_effect=_save),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # Discard the native conversation (exactly once) before persistence.
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    assert order == ["discard:dashboard:s1:True", "save"]
    # The live slot only adopts the edit after the boundaries commit.
    assert [m["content"] for m in slot.messages] == ["edited"]
    # The edited turn is dispatched after the commit.
    run.assert_awaited_once()
    assert run.await_args.args[2] == "edited"


@pytest.mark.asyncio
async def test_edit_resend_cancelled_mid_save_keeps_live_and_disk_in_sync(state) -> None:
    """A client disconnect during the save must not desync disk from the live
    slot. The worker thread finishes the destructive rewrite regardless of the
    handler's fate; on cancellation the handler waits for the worker's outcome,
    commits the live slot to match the persisted window, and still dispatches
    the edited prompt. Mirrors
    test_dashboard_chat_rewind::test_rewind_cancelled_mid_save_still_commits_the_landed_rewrite.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    save_started = threading.Event()
    release = threading.Event()
    saved_windows: list[list[str]] = []

    def _gated_save(_state, _slot, msgs_snapshot, **_kwargs):
        # Record the window the worker thread persisted to "disk", then block
        # so the test can cancel the handler while the save is in flight.
        saved_windows.append([m["content"] for m in msgs_snapshot])
        save_started.set()
        # BOUNDED, and the release below is in a ``finally``. This blocks a
        # thread in the DEFAULT executor, which the interpreter joins at exit --
        # so a gate that is never released does not fail this test, it hangs
        # interpreter shutdown for the whole worker. Neither half is redundant:
        # the ``finally`` covers a failure inside the ``with`` block, and the
        # timeout covers a failure that prevents the ``finally`` from running at
        # all (a hard kill of the awaiting task).
        release.wait(timeout=_GATE_TIMEOUT_SECS)
        return True

    run = AsyncMock()
    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                side_effect=_gated_save,
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            app = _make_regen_app(state)
            fake_request = make_mocked_request(
                "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
            )
            fake_request["app"] = ""

            async def _json():
                return {"index": 0, "content": "edited"}

            fake_request.json = _json  # type: ignore[method-assign]
            handler_task = asyncio.create_task(api_chat_slot_edit_resend(fake_request))
            # The INNER wait is bounded too, and that is the load-bearing half:
            # cancelling ``wait_for`` abandons the future but cannot interrupt the
            # worker thread already sitting in ``Event.wait()``, so an unbounded
            # inner wait strands a default-executor thread that interpreter
            # shutdown then joins on. The outer 2s stays tight so a save that
            # never starts still fails this test fast.
            await asyncio.wait_for(
                asyncio.to_thread(save_started.wait, _GATE_TIMEOUT_SECS), timeout=2
            )
            handler_task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await handler_task

            # The rewrite landed on "disk" with the truncated+edited window; the
            # live slot must have adopted the SAME window rather than keeping the
            # full original one (which the next flush would push back over disk).
            assert saved_windows == [["edited"]]
            assert [m["content"] for m in slot.messages] == ["edited"]
            # The edited prompt is still dispatched.
            for _ in range(50):
                if run.await_count:
                    break
                await asyncio.sleep(0.02)
            run.assert_awaited_once()
            assert run.await_args.args[2] == "edited"
    finally:
        # Unconditional: a failure anywhere above must still let the gated
        # worker thread exit, or it outlives this test and blocks the
        # interpreter's executor join at shutdown.
        release.set()


@pytest.mark.asyncio
async def test_edit_resend_excludes_the_periodic_flush_during_the_rewrite(state) -> None:
    """The periodic dirty-slot flush must not race the rewrite.

    The live slot keeps the FULL window until the commit, so a flush tick can
    snapshot that stale window, block behind this rewrite on the per-session
    history lock, and then write the snapshot back on top -- restoring every
    message the rewrite just discarded. Routing the save through
    ``save_slot_off_loop`` with an ``expected_history_key`` raises
    ``_metadata_persist_inflight``, which is the flag ``flush_slot_now`` already
    honours to skip a slot with a guarded write pending.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    observed: list[int] = []
    flush_ran: list[bool] = []

    def _save_observing_inflight(_state, saved_slot, *_args, **_kwargs):
        # Runs on the worker thread WHILE the guard should be held.
        observed.append(getattr(saved_slot, "_metadata_persist_inflight", 0))
        # A real flush tick landing here must decline to write this slot. Its
        # only observable "I declined" is leaving the dirty bit set, since a
        # completed flush clears it.
        state.flush_slot_now(saved_slot)
        flush_ran.append(bool(saved_slot._dirty))
        return True

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            side_effect=_save_observing_inflight,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # The guard was held for the duration of the rewrite ...
    assert observed and all(count > 0 for count in observed)
    # ... and the concurrent flush declined rather than writing the stale window
    # (it returned without clearing the dirty bit).
    assert flush_ran == [True]
    # Released afterwards, or the slot would never flush again.
    assert slot._metadata_persist_inflight == 0


@pytest.mark.asyncio
async def test_edit_resend_repeated_cancellation_still_commits_the_landed_rewrite(state) -> None:
    """A SECOND cancellation must not abandon the rewrite.

    A gateway shutdown can cancel a handler already unwinding from a client
    disconnect, and ``CancelledError`` is a ``BaseException`` -- so an
    ``except Exception`` around the drain cannot absorb it and a bare
    ``await save_task`` walks away from a rewrite the worker thread finishes
    anyway: disk truncated, live slot still holding the discarded suffix, and the
    next flush pushing that stale window back over the truncated file.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    save_started = threading.Event()
    release = threading.Event()

    def _gated_save(_state, _slot, msgs_snapshot, **_kwargs):
        save_started.set()
        release.wait(timeout=_GATE_TIMEOUT_SECS)
        return True

    run = AsyncMock()
    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                side_effect=_gated_save,
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            app = _make_regen_app(state)
            fake_request = make_mocked_request(
                "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
            )
            fake_request["app"] = ""

            async def _json():
                return {"index": 0, "content": "edited"}

            fake_request.json = _json  # type: ignore[method-assign]
            handler_task = asyncio.create_task(api_chat_slot_edit_resend(fake_request))
            await asyncio.wait_for(
                asyncio.to_thread(save_started.wait, _GATE_TIMEOUT_SECS), timeout=2
            )
            # First cancel: the client disconnected. The handler is now inside the
            # drain, waiting on the still-blocked worker.
            handler_task.cancel()
            await asyncio.sleep(0)
            # Second cancel: the gateway is shutting down. This is the one a bare
            # await loses.
            handler_task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await handler_task

            # Disk was rewritten, so the live slot MUST match it rather than
            # keeping the full original window.
            assert [m["content"] for m in slot.messages] == ["edited"]
            for _ in range(50):
                if run.await_count:
                    break
                await asyncio.sleep(0.02)
            run.assert_awaited_once()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_edit_resend_logs_a_failing_background_turn(state, caplog) -> None:
    """The task is fire-and-forget, so its exception must be surfaced by the
    done-callback or it is swallowed entirely."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat",
        new=AsyncMock(side_effect=RuntimeError("backend exploded")),
    ):
        with caplog.at_level("ERROR"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
                assert resp.status == 200
                await asyncio.sleep(0)
                await asyncio.sleep(0)

    assert "edit-resend _run_chat failed" in caplog.text


# ── edit-resend: the prospective copy must not touch the live slot ──
# ``copy.copy`` is shallow, so reassigning ``messages`` alone leaves every other
# mutable attribute aliased to the live slot's object -- and ``append`` writes
# through four of them. These pin that a REFUSED edit leaves all four alone, and
# that the commit is what adopts them.


def _arm_pending_question(slot) -> tuple[str, list]:
    """Register one non-blocking question card and a retirement spy on *slot*."""
    announced: list = []
    slot._question_pending = {"q1": {"blocking": False, "prompt": "which host?"}}
    slot._on_question_retired = lambda key, ids: announced.append((key, list(ids)))
    return "q1", announced


@pytest.mark.asyncio
async def test_edit_resend_refusal_leaves_the_live_pending_and_cards_alone(state) -> None:
    """A refused edit must publish nothing to the live slot.

    The prospective ``append`` runs BEFORE all four rejection points, so an
    un-severed shallow copy pushes the edited row into the live ``_pending``
    queue (the open stream reader's next drain renders it), wakes ``event``, and
    announces the live question cards as retired -- leaving a phantom row on
    screen and a card-less "needs input" behind for an edit the server refused.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.event.clear()
    question_id, announced = _arm_pending_question(slot)
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history"),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            await asyncio.sleep(0)

    assert slot._pending == []
    assert not slot.event.is_set()
    assert announced == []
    assert question_id in slot._question_pending


@pytest.mark.asyncio
async def test_edit_resend_commit_adopts_the_prepared_pending_and_retires_cards(state) -> None:
    """The commit is the ONE place the prepared state becomes live.

    Severing the copy must not lose the work: on success the edited row still
    reaches the live pending queue, ``event`` is still woken for the stream
    reader, and the question retirement the prospective append computed is
    announced HERE, through the live callback the copy was denied.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.event.clear()
    question_id, announced = _arm_pending_question(slot)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot._pending] == ["edited"]
    assert slot.event.is_set()
    assert announced == [("s1", [question_id])]
    assert question_id not in slot._question_pending


@pytest.mark.asyncio
async def test_edit_resend_commit_advances_the_lifetime_message_counter(state) -> None:
    """``total_messages`` is a lifetime counter, and the prospective ``append``
    bumps only the COPY's int.

    Left stale, the edited row is invisible to every reader of it:
    ``_get_active_workspace`` picks the max-counter slot to decide which
    workspace's lessons to load, and the Slack mirror compares the counter
    against its own start value to decide whether anything happened. A refused
    edit must not advance it either.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    before = slot.total_messages

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # One new row landed, so the lifetime counter advanced by exactly one --
    # truncating the window deliberately does not roll it back.
    assert slot.total_messages == before + 1

    # A refused edit leaves it alone.
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))
    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history"),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "again"}
            )
            assert resp.status == 503
            await asyncio.sleep(0)

    assert slot.total_messages == before + 1


# ── edit-resend: app isolation ──
# This endpoint discards the slot's NATIVE ACP conversation, so an app token
# reaching a slot it does not own destroys a resume identity it has no claim on.


def _app_request(state, slot_name: str, app_token: str, body: dict):
    """A mocked edit-resend request carrying *app_token* as its app identity."""
    request = make_mocked_request(
        "POST",
        f"/api/chat/slots/{slot_name}/edit-resend",
        match_info={"slot": slot_name},
        app=_make_regen_app(state),
    )
    request["app"] = app_token

    async def _json():
        return body

    request.json = _json  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_edit_resend_denies_an_app_that_does_not_own_the_slot(state) -> None:
    """404 rather than 403: a non-owning token must not be able to use the status
    code to probe which slots exist. Nothing is discarded and nothing moves."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(
            _app_request(state, "s1", "other-app", {"index": 0, "content": "edited"})
        )

    assert resp.status == 404
    assert resp.text is not None and "slot_not_found" in resp.text
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_denies_an_app_reaching_a_channel_linked_session(state) -> None:
    """Owning the slot is not owning the session it is linked to.

    ``effective_session_key`` resolves a channel-linked slot onto the channel's
    own conversation, so an app edit-resend would discard the native identity of
    a session the app does not own. Same 404 shape (anti-enumeration).
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._app = "some-app"
    slot.linked_session_key = "slack:1234567890.123"
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(
            _app_request(state, "s1", "some-app", {"index": 0, "content": "edited"})
        )

    assert resp.status == 404
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_reauthorizes_the_slot_after_the_body_read(state) -> None:
    """Reading the body is an await, and a slot can be replaced across it.

    A delete-and-recreate under the same name is a DIFFERENT conversation that
    would pass any name-based re-check, so the guard requires the same slot
    OBJECT. Not app-only: a dashboard caller must not land a destructive edit on
    a replaced slot either.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    request = make_mocked_request(
        "POST",
        "/api/chat/slots/s1/edit-resend",
        match_info={"slot": "s1"},
        app=_make_regen_app(state),
    )
    request["app"] = ""

    async def _json():
        # The replacement lands while the body is being read.
        state._slots["s1"] = state.get_or_create_slot("s2")
        return {"index": 0, "content": "edited"}

    request.json = _json  # type: ignore[method-assign]

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        resp = await api_chat_slot_edit_resend(request)

    assert resp.status == 404
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


# ── edit-resend: a busy SESSION is not the same question as a busy slot ──
# ``discard_conversation`` is a full teardown: it drops the native conversation
# AND releases the shared sub-agent runtime. ``slot.running`` tracks only this
# slot's own task, so it answers False in both states below.


@pytest.mark.asyncio
async def test_edit_resend_refuses_while_a_plan_is_mid_stage(state) -> None:
    """An autopilot plan reads ``running`` False BETWEEN stages while still
    mid-plan, so ``running`` alone would discard the conversation the plan is
    writing into and truncate the history it is producing. Same 409 code the
    sibling reset-conversation teardown returns."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._in_stage_execution = True
    original_messages = list(slot.messages)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_orchestrating"

    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_while_subagents_are_attached(state) -> None:
    """The discard releases the shared runtime the parent's children run on, and
    the parent turn ends FIRST -- so ``running`` is False while they keep going.
    Without this guard an edit destroys work it has no part in."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=["child-1"])
    subs._queued_depth = MagicMock(return_value=0)
    state.subagents = subs

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "slot_subagents_running"

    # Probed on the session the discard would have torn down, not on the slot name.
    subs.running_agents_for.assert_called_with("dashboard:s1")
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_when_the_subagent_probe_fails(state) -> None:
    """An unreadable probe is UNKNOWN children, not zero children. The shared
    predicate fails closed on a None running-probe; pinning it here keeps this
    endpoint from being the one that reads a failure as "safe to tear down"."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=None)
    state.subagents = subs

    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "slot_subagents_running"

    state.sessions.discard_conversation.assert_not_awaited()


# ── edit-resend: the slot is reserved across the durable boundaries ──


@pytest.mark.asyncio
async def test_edit_resend_reserves_the_slot_so_a_concurrent_send_queues(state) -> None:
    """``slot.running`` must read True while the boundaries are pending.

    ``running`` derives from ``slot.task`` and the send path is not serialized on
    ``slot._lock``, so without the reservation a send arriving during the three
    awaited boundaries sees an idle slot, appends its row and dispatches a
    competing turn -- which the commit would then erase. Reserved, that send
    takes the queue path instead, and the commit leaves the entry alone.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    observed: dict = {}

    async def _discard(key, **kwargs):
        observed["running"] = slot.running
        observed["arrived_id"] = slot.queue_append("sent during the edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert observed["running"] is True
    assert observed["arrived_id"] in [entry["id"] for entry in slot._queue]


@pytest.mark.asyncio
async def test_edit_resend_abort_hands_a_diverted_send_to_the_queue_drain(state) -> None:
    """A send diverted by the reservation must never be stranded.

    On abort no turn ran, so the entry the reservation pushed to the queue has
    no drain trigger of its own; the reserved task hands it to the canonical
    successor dispatch, which re-validates holds before starting anything.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        slot.queue_append("sent during the edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    drain = AsyncMock(return_value=True)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            for _ in range(50):
                if drain.await_count:
                    break
                await asyncio.sleep(0.02)

    drain.assert_awaited_once()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_abort_leaves_a_pre_existing_queue_entry_waiting(state) -> None:
    """An entry queued BEFORE the reservation keeps its own trigger.

    Only a send DIVERTED by this reservation lost its drain, so an abort must not
    dispatch on behalf of work that was already waiting -- that would start a
    turn the user never unblocked.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot.queue_append("queued long before the edit")
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))
    drain = AsyncMock(return_value=True)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run,
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    drain.assert_not_awaited()
    run.assert_not_awaited()
    assert len(slot._queue) == 1


# ── edit-resend: rows that arrive during the boundary belong to the new timeline ──


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_row_injected_during_the_boundary(state) -> None:
    """A workflow/cron completion landing mid-boundary must survive the commit.

    Those injectors append WITHOUT taking ``slot._lock`` -- ``workflow_inject``
    calls ``append_and_surface`` straight on the event loop -- so a wholesale
    ``slot.messages = prospective_slot.messages`` silently drops the injected
    row. The rewrite save cannot put it back either: a rewrite deliberately
    skips the cross-process-append scan, so carrying it in the live window is
    what keeps it. It reaches disk on the next ordinary flush, which is why the
    boundary itself writes exactly ONCE.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    saved_windows: list[list[str]] = []

    async def _discard(key, **kwargs):
        # Stands in for inject_workflow_result: an append on the live slot while
        # this handler holds slot._lock.
        slot.append("assistant", "workflow finished", "msg msg-a")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    def _save(_state, saved_slot, msgs_snapshot=None, **_kwargs):
        window = msgs_snapshot if msgs_snapshot is not None else saved_slot.messages
        saved_windows.append([m["content"] for m in window])
        return True

    with (
        patch("kiro_crew.dashboard.chat_persistence._save_slot_to_history", side_effect=_save),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    # The truncation still happened, the edit is live, and the injected row was
    # carried rather than replaced away.
    assert [m["content"] for m in slot.messages] == ["edited", "workflow finished"]
    assert "workflow finished" in [m["content"] for m in slot._pending]
    # Order is not accidental, and the claim is "never EARLIER" rather than
    # "strictly later". ``monotonic_transcript_ts`` only ever moves a row
    # forward, and it floors on the window tail the appender saw: the edited row
    # was floored on the (empty) truncated prefix, the arrived row on the
    # original tail. On a coarse clock -- Windows advances the system clock in
    # ~15.6 ms steps, which is exactly why that helper exists -- both reads of
    # ``now`` return the same instant and the two rows legitimately carry an
    # IDENTICAL ts. Asserting ``<`` passed on Linux and failed on Windows for
    # that reason. What must hold is that the merge never stamps the arrived row
    # BEFORE the edited one, which would reorder the transcript; list order is
    # what separates a tie, and the content assertion above pins that.
    assert slot.messages[0]["ts"] <= slot.messages[1]["ts"]
    # The boundary writes ONCE, and the carried row is left to the ordinary flush.
    # A second guarded save for it would have to be awaited after the commit,
    # where ``dispatch_commit`` is already True and only ``dispatch_ready.set()``
    # remains -- so a rebind landing on that await would release this handler's
    # prompt against another conversation. ``_dirty`` is what makes the merged
    # window durable instead.
    assert saved_windows == [["edited"]]
    assert slot._dirty is True


@pytest.mark.asyncio
async def test_edit_resend_commit_does_not_resurrect_a_card_retired_meanwhile(state) -> None:
    """The prospective question map is PRE-await, so adopting it wholesale would
    restore a card an arrived row already retired -- re-rendering a card whose
    answer channel is gone. The commit intersects instead: retired by either
    side stays retired."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    slot._question_pending = {
        "q1": {"blocking": False, "prompt": "which host?"},
        "q2": {"blocking": False, "prompt": "which region?"},
    }
    announced: list = []
    slot._on_question_retired = lambda key, ids: announced.append((key, sorted(ids)))

    async def _discard(key, **kwargs):
        # An arrived user-role row retires every live non-blocking card.
        slot.append("user", "asked elsewhere")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert slot._question_pending == {}
    # The arrived append already announced the retirement; the commit must not
    # announce the same ids a second time.
    assert announced == [("s1", ["q1", "q2"])]


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_blocking_card_answered_meanwhile(state) -> None:
    """An answer landing mid-boundary must not be undone by the commit.

    Answering pops the id from the LIVE dict, so a commit that assigned the
    frozen pre-await copy back would restore the card with its answer channel
    already gone. A BLOCKING card is the shape that reaches this: an append never
    retires one, so the edit's own ``user`` row cannot be what cleared it.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    state.mark_question_pending("s1", blocking=True, card_id="ask-1")
    assert "ask-1" in slot._question_pending
    observed: dict = {}

    async def _discard(key, **kwargs):
        observed["cleared"] = state.clear_question_pending("s1", card_id="ask-1")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert observed["cleared"] is True
    assert "ask-1" not in slot._question_pending, (
        "the commit resurrected a card that was answered during the boundary, "
        "so the slot awaits input against a completed round-trip"
    )


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_a_card_that_arrived_meanwhile(state) -> None:
    """A card marked mid-boundary must survive the commit.

    ``post_question_card`` is addressed by slot key, so a card can be marked on a
    slot whose turn is being replaced. It is in the LIVE dict and not in the
    frozen pre-await copy, so any commit keyed on that copy erases it -- and the
    client has already been shown it.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard(key, **kwargs):
        state.mark_question_pending("s1", blocking=True, card_id="ask-late")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert "ask-late" in slot._question_pending, (
        "the commit erased a card that was raised during the boundary, so the "
        "client renders a card the server no longer believes is pending"
    )


# ── edit-resend: the commit re-checks the transcript it was authorized against ──


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_rebound(state) -> None:
    """A slot rebound to another transcript mid-save must not be replaced.

    A cron injection can re-link the slot -- hydrating it with another
    conversation's state -- while the history write is in flight. The save's own
    ``expected_history_key`` guard cannot see it, because the snapshot froze the
    old routing; this loop-side re-check is the only fence that can, and without
    it the commit silently overwrites the injected conversation.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    async def _rebinding_discard(key, **kwargs):
        # The slot moves to another transcript while the edit persists. The
        # save is stubbed to accept the write, so ONLY the commit-side re-check
        # can refuse -- which is exactly what this pins.
        slot.linked_session_key = "slack:9876543210.999"
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_rebinding_discard)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=True),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_slot_rebound"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_replaced(state) -> None:
    """A close-and-recreate under the same name is a DIFFERENT conversation.

    The transcript key is unchanged by such a swap, so the rebind fence cannot
    see it; identity has to be the slot OBJECT -- the same discipline
    ``_reauthorize_after_await`` applies across the body-read await.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    async def _replacing_discard(key, **kwargs):
        # Same name, different object -- as a close-and-recreate produces.
        state._slots["s1"] = state.get_or_create_slot("s2")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_replacing_discard)
    run = AsyncMock()

    with (
        patch(
            "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
            MagicMock(return_value=True),
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "edit_resend_slot_rebound"
            await asyncio.sleep(0)

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_reservation_is_displaced(state) -> None:
    """If something else took ``slot.task``, committing would run this handler's
    turn ALONGSIDE whatever now owns the slot -- two concurrent turns writing one
    window. The reservation must still be the slot's task at commit time."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    usurpers: list = []

    async def _displacing_discard(key, **kwargs):
        async def _other_turn() -> None:
            await asyncio.sleep(10)

        # Another dispatcher claims the slot while the boundary is pending.
        usurpers.append(asyncio.create_task(_other_turn()))
        slot.task = usurpers[-1]
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_displacing_discard)
    run = AsyncMock()

    try:
        with (
            patch(
                "kiro_crew.dashboard.chat_persistence._save_slot_to_history",
                MagicMock(return_value=True),
            ),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run),
        ):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
                )
                assert resp.status == 503
                assert (await resp.json())["code"] == "edit_resend_slot_rebound"
                await asyncio.sleep(0)

        assert slot.messages == original_messages
        run.assert_not_awaited()
    finally:
        for pending in usurpers:
            pending.cancel()


# ── machine-readable refusal codes ──
# The tests above pin each refusal's human sentence. These pin the `code`
# beside it, which is the half a caller can branch on: "slot is running" is a
# developer sentence that a client must not string-match to tell a BUSY slot
# (retry once the turn ends) from a MISSING one (stop and refresh).


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("regenerate", None),
        ("switch-variant", {"index": 0}),
        ("edit-resend", {"index": 0, "content": "edited"}),
    ],
)
@pytest.mark.asyncio
async def test_every_endpoint_refuses_a_busy_slot_with_slot_running(state, path, body) -> None:
    """All three endpoints share one busy-slot refusal, so they must share one
    code -- a client that special-cases the retryable case cannot be asked to
    learn a different spelling per endpoint."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(f"/api/chat/slots/s1/{path}", json=body)
            assert resp.status == 409
            payload = await resp.json()
            assert payload["code"] == "slot_running"
            # The human sentence is unchanged: the code is additive, so an
            # existing client that renders `error` keeps working.
            assert payload["error"] == "slot is running"
    finally:
        slot.task.cancel()


@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        ("regenerate", None, 404, "slot_not_found"),
        ("switch-variant", {"index": 0}, 404, "slot_not_found"),
        ("edit-resend", {"index": 0, "content": "x"}, 404, "slot_not_found"),
    ],
)
@pytest.mark.asyncio
async def test_unknown_slot_refusals_carry_slot_not_found(state, path, body, status, code) -> None:
    async with _client(state) as client:
        resp = await client.post(f"/api/chat/slots/nope/{path}", json=body)
        assert resp.status == status
        assert (await resp.json())["code"] == code


@pytest.mark.asyncio
async def test_no_variants_refusal_carries_its_own_code(state) -> None:
    """Distinct from a busy slot: nothing to switch to is permanent for this
    row, so a client must not offer a retry."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "only reply")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        payload = await resp.json()
        assert payload["code"] == "no_variants"
        assert payload["error"] == "no variants"


@pytest.mark.asyncio
async def test_restore_variant_idx_names_the_partial_when_content_matches_an_old_variant(
    state,
) -> None:
    """F2: when a regenerate ends with a PARTIAL reply whose content equals one
    of the old reply's prior variants, the active variant_idx must still name the
    partial (the active, newest variant), not the matching older entry.

    The partial is always appended before the cap (mirroring _flush_segment), so
    variant_idx == len(variants) - 1 names the partial and the row's adopted
    content agrees with the variant that index points at. A content-dedup that
    skipped the append would leave variant_idx pointing at the older match while
    the row shows the partial — the active-variant-index corruption this guards.
    """
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    # The old reply carries two prior variants; the FIRST ("MATCH") is the one the
    # partial will match by content, and a DIFFERENT variant ("OTHER") sorts after
    # it — so under the dedup-skip bug the active index (len-1) would land on
    # "OTHER" while the row adopted "MATCH".
    slot.messages.append(
        {
            "role": "assistant",
            "content": "OTHER",
            "cls": "msg msg-a",
            "meta": {"mid": "mid-old"},
            "variants": [
                {"role": "assistant", "content": "MATCH"},
                {"role": "assistant", "content": "OTHER"},
            ],
            "variant_idx": 1,
        }
    )
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    # The regenerated turn produces a PARTIAL reply whose content ("MATCH") equals
    # an EARLIER existing variant — the dedup-match case that corrupts the index.
    async def _partial_dup_turn(*a, **kw):
        partial = {"role": "assistant", "content": "MATCH", "meta": {"mid": "mid-partial"}}
        slot.messages.append(partial)
        if not isinstance(getattr(slot, "_turn_reply_mids_all", None), list):
            slot._turn_reply_mids_all = []
        slot._turn_reply_mids_all.append("mid-partial")
        return None

    async def _saves_commit(*a, **kw):
        return True

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_partial_dup_turn),
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_saves_commit),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            rt = slot._regenerate_restore_task
            if rt is not None:
                await rt

    partial_row = next(
        (m for m in slot.messages if m.get("meta", {}).get("mid") == "mid-partial"), None
    )
    assert partial_row is not None, "the partial reply row must be on the live window"
    variants = partial_row.get("variants")
    assert isinstance(variants, list) and variants, "the partial must carry attached variants"
    idx = partial_row.get("variant_idx")
    # The active index must point at the LAST element (the partial), and that
    # element's content must equal the row's adopted content — the integrity the
    # dedup-skip would have broken.
    assert (
        idx == len(variants) - 1
    ), f"variant_idx must name the active (last) variant, got {idx} of {len(variants)}"
    assert variants[idx].get("content") == partial_row.get(
        "content"
    ), "the variant the active index names must match the row's adopted content"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_shutdown_drain_settles_attach_before_writing_so_no_duplicate_reply(
    state,
) -> None:
    """F1: a shutdown landing mid-regenerate must not drain-then-attach in an
    order that persists the recovered reply TWICE.

    The restore's recovery write is held in-flight (its entry still registered)
    when the drain runs. The drain settles that restore task first and then
    re-reads the registry: the settled restore has already recovered the reply
    and dropped the entry, so the drain writes nothing and the reply lands
    exactly once. Without the settle gate the drain would write while the restore
    is suspended and the restore would write again — a duplicate.
    """
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    async def _empty_turn_then_rebind(*a, **kw):
        # Force the restore down _recover_to_original_transcript (slot rebound),
        # which writes the reply to the original transcript and clears the entry.
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    saves = {"n": 0}

    async def _truncate_ok_then_refuse(*a, **kw):
        saves["n"] += 1
        return saves["n"] == 1  # truncation commits; later slot saves refuse

    # Hold the restore's recovery write suspended so it is genuinely in-flight
    # (entry still registered) when the drain runs. Count every write to the
    # original transcript so a duplicate is detectable.
    real_restore = _cr.restore_full_rows_off_loop
    gate_release = asyncio.Event()
    writes = {"n": 0}
    first_write_entered = asyncio.Event()

    async def _blocking_first_write(conversation_log, key, rows, **kw):
        writes["n"] += 1
        if writes["n"] == 1:
            # The restore's write: signal it is in-flight, then block until the
            # drain has had its chance to (wrongly) write.
            first_write_entered.set()
            await gate_release.wait()
        return await real_restore(conversation_log, key, rows, **kw)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn_then_rebind),
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_refuse,
        ),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_blocking_first_write,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            # Wait until the restore's recovery write is in-flight and suspended,
            # with the entry still registered.
            await asyncio.wait_for(first_write_entered.wait(), timeout=5)
            assert _cr._PENDING_RECOVERIES, "the recovery entry must still be registered in-flight"
            # SHUTDOWN now. The drain must settle the in-flight restore (release it
            # so it commits and drops the entry) before considering a write — then
            # find the entry gone and write nothing.
            drain = asyncio.create_task(_cr.drain_pending_regenerate_recoveries())
            await asyncio.sleep(0)
            gate_release.set()  # let the restore's suspended write complete
            await drain
            rt = slot._regenerate_restore_task
            if rt is not None:
                try:
                    await rt
                except Exception:
                    pass

    rows = state.conversation_log.read_messages(original_key)
    recovered = [m for m in rows if m.get("content") == "ORIGINAL-REPLY"]
    assert len(recovered) == 1, (
        "the reply must be persisted exactly once — the drain settled the in-flight "
        f"restore and skipped the cleared entry (found {len(recovered)})"
    )
    # The drain performed NO recovery write of its own: the restore's single
    # write is the only one. A blind drain-ahead-of-settle would make a second,
    # redundant write (writes['n'] == 2) even where the full-rows rewrite is
    # content-idempotent — this measures the ordering defect directly.
    assert writes["n"] == 1, (
        "the drain must not issue a redundant recovery write while the restore is "
        f"in-flight; it settled and skipped (writes={writes['n']})"
    )
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_refuses_when_recovery_payload_exceeds_bound_reply_intact(
    state,
) -> None:
    """:99 admission: a reply whose serialized recovery payload exceeds the named
    byte bound must REFUSE the regenerate (409) with the previous reply left
    intact on the window and transcript — the truncation never runs.

    This is the non-circular remedy: admission is reserved BEFORE the destructive
    mutation, so an un-retainable recovery refuses rather than truncating first.
    """
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")
    state.conversation_log.append(original_key, "assistant", "ORIGINAL-REPLY")

    before = slot.messages[-1]
    saves = {"n": 0}

    async def _record_saves(*a, **kw):
        saves["n"] += 1
        return True

    # Force the payload bound tiny so an ordinary reply trips it.
    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_record_saves),
        patch.object(_cr, "_MAX_RECOVERY_PAYLOAD_BYTES", 1),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 409
            payload = await resp.json()
            assert payload["code"] == "recovery_payload_too_large"

    # Nothing was truncated: the previous reply is still the last row, no save ran,
    # and nothing was registered for a drain.
    assert slot.messages[-1] is before, "the previous reply must remain intact on the window"
    assert saves["n"] == 0, "no truncating save may run when admission is refused"
    assert not _cr._PENDING_RECOVERIES, "a refused admission must register no recovery"
    rows = state.conversation_log.read_messages(original_key)
    assert any(
        m.get("content") == "ORIGINAL-REPLY" for m in rows
    ), "the previous reply must remain on the transcript"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_regenerate_refuses_when_recovery_registry_is_full_reply_intact(state) -> None:
    """:99 admission (capacity arm): when the registry is already at its count
    cap, a regenerate must REFUSE rather than FIFO-drop an older owed reply to
    make room — reserve-before-truncate keeps the previous reply intact."""
    from kiro_crew.dashboard import chat_regenerate as _cr

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    before = slot.messages[-1]

    saves = {"n": 0}

    async def _record_saves(*a, **kw):
        saves["n"] += 1
        return True

    # Fill the registry to its cap so there is no capacity to reserve.
    with (
        patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_record_saves),
        patch.object(_cr, "_PENDING_RECOVERIES_MAX", 2),
    ):
        _cr._PENDING_RECOVERIES["a#regen1"] = (object(), "dashboard:a", [{"content": "x"}], True)
        _cr._PENDING_RECOVERIES["b#regen2"] = (object(), "dashboard:b", [{"content": "y"}], True)
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 409
            payload = await resp.json()
            assert payload["code"] == "recovery_capacity_exhausted"

    assert slot.messages[-1] is before, "the previous reply must remain intact on the window"
    assert saves["n"] == 0, "no truncating save may run when capacity is exhausted"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_partial_attach_rebind_does_not_leak_old_variants_to_new_transcript(
    state, caplog
) -> None:
    """F1: an abnormally-ended regenerate (partial reply, _pending_variants still
    set) whose variant-attach save finds the slot REBOUND to a different
    conversation must not leak the previous conversation's variants into the
    newly-bound transcript.

    The partial-branch rebound path recovers the old reply to the ORIGINAL
    transcript and reverts the partial row's attached variants. The revert runs
    BEFORE the recovery await, not after, because the await suspends while the
    slot is already bound to the NEW conversation: variants left on the row
    across that suspension would be persisted into the newly-bound transcript by
    a periodic flush (a cross-transcript variant leak). Reverting first means the
    row carries nothing of the old conversation while recovery is in flight.

    This test observes the row's attached-variant state AT the recovery await
    (the exact instant a flush could run) and asserts the old reply is absent
    from the partial row's variants there. With the revert ordered after the
    await the old reply is still attached at that instant; with it ordered
    before, the row is already clean -- so the assertion distinguishes the two
    orderings."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    async def _partial_reply_turn(*a, **kw):
        partial = {"role": "assistant", "content": "PARTIAL-REPLY", "meta": {"mid": "mid-partial"}}
        slot.messages.append(partial)
        if not isinstance(getattr(slot, "_turn_reply_mids_all", None), list):
            slot._turn_reply_mids_all = []
        slot._turn_reply_mids_all.append("mid-partial")
        return None

    saves = {"n": 0}

    async def _truncate_ok_then_attach_rebinds(*a, **kw):
        saves["n"] += 1
        if saves["n"] == 1:
            return True  # endpoint truncating write commits
        # The variant-attach save: the slot is rebound to a DIFFERENT
        # conversation during the await (a cron/reconciler), and the save still
        # commits (its pin matched the old key when it ran). This drives the
        # partial-branch rebound recovery path.
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return True

    # Snapshot the partial row's variant contents at the instant recovery is
    # awaited -- this is the window a periodic flush would persist them to the
    # newly-bound transcript, so what the row carries HERE is what could leak.
    real_restore = _cr.restore_full_rows_off_loop
    seen_at_recovery = {}

    async def _restore_capturing_variants(conv_log, key, rows, *a, **kw):
        partial = next((m for m in slot.messages if m.get("content") == "PARTIAL-REPLY"), None)
        seen_at_recovery["variant_contents"] = [
            v.get("content") for v in ((partial or {}).get("variants") or [])
        ]
        return await real_restore(conv_log, key, rows, *a, **kw)

    with (
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_partial_reply_turn),
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_attach_rebinds,
        ),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_restore_capturing_variants,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            rt = slot._regenerate_restore_task
            if rt is not None:
                try:
                    await rt
                except Exception:
                    pass
            await asyncio.sleep(0.05)

    # At the recovery await, the partial row must NOT still carry the old
    # conversation's reply as a variant -- it must have been reverted FIRST, so a
    # flush during recovery cannot persist it into the rebound transcript.
    assert "variant_contents" in seen_at_recovery, "the recovery path must have been awaited"
    assert "ORIGINAL-REPLY" not in seen_at_recovery["variant_contents"], (
        "the previous conversation's reply must be stripped from the partial row's variants "
        "BEFORE the recovery await, so a flush during the await cannot leak it into the "
        f"newly-bound transcript (saw variants={seen_at_recovery['variant_contents']})"
    )
    # And the old reply landed on the ORIGINAL transcript (recovered), not on the
    # rebound conversation.
    other_rows = state.conversation_log.read_messages("dashboard:cron-other-conversation")
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in other_rows
    ), "the old reply must not leak onto the rebound conversation's transcript"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_concurrent_regenerate_second_refused_at_admission_no_unpaid_eviction(
    state, caplog
) -> None:
    """F2: two concurrent regenerates at the registry's capacity boundary must not
    both pass admission and then evict an already-owed (unpaid) recovery.

    _recovery_admission_refusal reads len(_PENDING_RECOVERIES) for headroom, but
    the entry it admits is not registered until many awaits later. A synchronous
    reservation closes the gap: without one, two regenerates both pass the
    headroom check at the MAX-1 boundary and both later register, overflowing the
    cap and FIFO-dropping an unpaid older recovery. The reservation makes
    regenerate #1 hold one across its truncating-save await, so #2 counts entries
    PLUS that reservation and is refused at admission with its previous reply
    left intact and no truncating save run.

    The assertion distinguishes the two: counting only live entries lets #2 pass
    admission and truncate; counting the reservation refuses #2 with a 409
    recovery_capacity_exhausted and leaves every owed entry intact."""
    from kiro_crew.dashboard import chat_regenerate as _cr

    _cr._PENDING_RECOVERIES.clear()
    _cr._RESERVED_RECOVERIES = 0
    # Tiny cap so the boundary is cheap to construct: fill to MAX-1 real entries,
    # leaving exactly one admission slot.
    monkeypatch_max = 3
    _orig_max = _cr._PENDING_RECOVERIES_MAX
    _cr._PENDING_RECOVERIES_MAX = monkeypatch_max
    try:
        for n in range(monkeypatch_max - 1):  # MAX-1 owed entries
            _cr._PENDING_RECOVERIES[f"dashboard:owed{n}#r{n}"] = (
                object(),
                f"dashboard:owed{n}",
                [{"role": "assistant", "content": f"owed{n}"}],
                True,
            )
        assert len(_cr._PENDING_RECOVERIES) == monkeypatch_max - 1

        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "REPLY-ONE")
        slot.drain()

        slot2 = state.get_or_create_slot("s2")
        slot2.append("user", "hi2")
        slot2.append("assistant", "REPLY-TWO")
        slot2.drain()

        # Regenerate #1's truncating save blocks, holding its admission
        # reservation across the await -- the exact window #2 must observe.
        regen1_save_entered = asyncio.Event()
        release_regen1_save = asyncio.Event()
        saves = {"s1": 0, "s2": 0}

        async def _save(state_arg, slot_arg, *a, **kw):
            key = "s1" if slot_arg is slot else "s2"
            saves[key] += 1
            if key == "s1":
                regen1_save_entered.set()
                await release_regen1_save.wait()
            return True

        async def _empty_turn(*a, **kw):
            return None

        with (
            patch("kiro_crew.dashboard.chat_regenerate.save_slot_off_loop", new=_save),
            patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
        ):
            async with _client(state) as client:
                # Start #1; it reserves (now MAX-1 entries + 1 reserved = MAX) and
                # blocks in its truncating save, holding the reservation.
                r1 = asyncio.create_task(client.post("/api/chat/slots/s1/regenerate"))
                await asyncio.wait_for(regen1_save_entered.wait(), timeout=5)

                # #2 attempts admission while #1's reservation is held: it must be
                # refused rather than passing and truncating.
                before2 = slot2.messages[-1]
                resp2 = await client.post("/api/chat/slots/s2/regenerate")
                assert resp2.status == 409, "the second regenerate must be refused at admission"
                payload2 = await resp2.json()
                assert payload2["code"] == "recovery_capacity_exhausted"
                assert slot2.messages[-1] is before2, "slot2's previous reply must remain intact"
                assert saves["s2"] == 0, "no truncating save may run for the refused regenerate"

                # Release #1; it completes normally.
                release_regen1_save.set()
                resp1 = await r1
                assert resp1.status == 200
                if slot.task is not None:
                    await slot.task
                await asyncio.sleep(0)

        # No unpaid eviction: the MAX-1 pre-seeded owed entries are all still
        # present (none were FIFO-dropped to make room for an over-admitted #2).
        for n in range(monkeypatch_max - 1):
            assert (
                f"dashboard:owed{n}#r{n}" in _cr._PENDING_RECOVERIES
            ), f"owed entry {n} must not be evicted by an over-admitted concurrent regenerate"
        # The reservation was released once #1 registered its entry.
        assert _cr._RESERVED_RECOVERIES == 0, "the reservation must be released after registration"
    finally:
        _cr._PENDING_RECOVERIES_MAX = _orig_max
        _cr._PENDING_RECOVERIES.clear()
        _cr._RESERVED_RECOVERIES = 0


@pytest.mark.asyncio
async def test_drain_does_not_reinsert_into_a_deleted_then_recreated_transcript(
    state, caplog
) -> None:
    """F1 (history.py deletion-generation fence): a failed-regenerate recovery is
    pending; the user DELETES the Slack history and then posts to the same thread,
    recreating the SAME-NAME transcript file. The shutdown drain must NOT reinsert
    the deleted reply into the recreated session.

    The old existence check was blind to this: it only honored a delete when the
    file was ABSENT, so a recreated same-name file (present again) slipped past it
    and the stale reply was written into whatever now held the name. The
    deletion-generation captured at truncation, rechecked under the write lock, is
    bumped by the delete regardless of the recreate, so the recovery honors the
    delete. Pre-fix (no generation fence) the recreated transcript receives the
    stale 'ORIGINAL-REPLY'; post-fix it does not."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    saves = []

    async def _truncate_ok_then_rebind_and_refuse(*a, **kw):
        # First save (the truncation) commits; the second (the variant-attach)
        # finds the slot rebound and refuses, driving the recovery to the
        # original transcript so a pending entry is registered for the drain.
        saves.append(kw)
        if len(saves) == 1:
            return True
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return False

    async def _empty_turn(*a, **kw):
        return None

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate.save_slot_off_loop",
            new=_truncate_ok_then_rebind_and_refuse,
        ),
        patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_empty_turn),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            if slot.task is not None:
                await slot.task
            await asyncio.sleep(0)
            # A recovery is pending for original_key. Now the user DELETES the
            # session and POSTS to the same thread, recreating the same-name
            # transcript. Cancel any armed retry so the drain is the writer.
            rt = slot._regenerate_restore_task
            if rt is not None:
                rt.cancel()
            assert any(
                v[1] == original_key for v in _cr._PENDING_RECOVERIES.values()
            ), "the recovery must be pending before the delete+recreate"
            state.conversation_log.delete_session(original_key)
            # Recreate the SAME-NAME transcript with fresh, unrelated content —
            # the user's new post to the recreated thread.
            state.conversation_log.append(original_key, "user", "a brand new conversation")
            await _cr.drain_pending_regenerate_recoveries()

    rows = state.conversation_log.read_messages(original_key)
    assert not any(
        m.get("content") == "ORIGINAL-REPLY" for m in rows
    ), "the drain must NOT reinsert the deleted reply into the recreated same-name transcript"
    assert any(
        m.get("content") == "a brand new conversation" for m in rows
    ), "the recreated transcript's own content must be untouched"
    _cr._PENDING_RECOVERIES.clear()


@pytest.mark.asyncio
async def test_shutdown_before_first_segment_does_not_duplicate_the_old_reply(
    state, caplog
) -> None:
    """F2 (chat_regenerate.py quiesce): a shutdown lands while a regenerate turn
    is still streaming BEFORE its first segment. The drain must QUIESCE the
    in-flight turn (cancel + await its done-callback's settle) before writing,
    rather than issuing its OWN standalone recovery write while the live turn's
    replacement save also persists the row — the double-write the finding names.

    The observable is the drain's own recovery write. Pre-fix _settle_gate
    returned immediately for an in-flight turn, so the drain called
    restore_full_rows_off_loop itself (writing the old reply standalone) WHILE
    the turn was still live and owed its own replacement save — two writers.
    Post-fix the drain cancels the turn, lets its done-callback's restore settle
    (one write, entry dropped), then re-reads the registry and writes nothing of
    its own. The restore is the single writer; the drain adds none."""
    from kiro_crew.dashboard import chat_regenerate as _cr
    from kiro_crew.dashboard.chat_utils import slot_history_key

    _cr._PENDING_RECOVERIES.clear()

    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "ORIGINAL-REPLY")
    slot.drain()
    original_key = slot_history_key(slot)
    state.conversation_log.append(original_key, "user", "hi")

    turn_running = asyncio.Event()
    release_turn = asyncio.Event()

    async def _turn_blocks_then_rebinds(*a, **kw):
        # In-flight before any segment while the drain fires; on release the slot
        # is rebound (no reply), so the done-callback drives the restore down
        # _recover_to_original_transcript — the one path that calls
        # restore_full_rows_off_loop, so the writer is countable.
        turn_running.set()
        await release_turn.wait()
        slot.linked_session_key = "dashboard:cron-other-conversation"
        return None

    real_restore = _cr.restore_full_rows_off_loop
    # Record, for every recovery write, whether the regenerate turn was STILL
    # in-flight (not done) at the moment of the write. Pre-fix the drain writes
    # the pre-turn recovery while the turn is still live (turn not done) — the
    # double-writer hazard. Post-fix the drain quiesces (cancels + awaits) the
    # turn FIRST, so any recovery write happens only once the turn is done.
    writes_while_turn_live = {"n": 0}
    writes_total = {"n": 0}

    async def _observing_restore(conversation_log, key, rows, **kw):
        writes_total["n"] += 1
        t = slot.task
        if t is not None and not t.done():
            writes_while_turn_live["n"] += 1
        return await real_restore(conversation_log, key, rows, **kw)

    with (
        patch(
            "kiro_crew.dashboard.chat_regenerate._run_chat",
            new=_turn_blocks_then_rebinds,
        ),
        patch(
            "kiro_crew.dashboard.chat_regenerate.restore_full_rows_off_loop",
            new=_observing_restore,
        ),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            await asyncio.wait_for(turn_running.wait(), timeout=5)
            assert any(
                v[1] == original_key for v in _cr._PENDING_RECOVERIES.values()
            ), "the removed reply must be pending before the shutdown drain"
            # Shutdown mid-stream-before-first-segment: drain WHILE the turn is
            # in-flight. Post-fix the quiesce cancels+awaits the turn before any
            # recovery write; pre-fix the drain wrote the pre-turn entry here with
            # the turn still live, then the released turn wrote again.
            await _cr.drain_pending_regenerate_recoveries()
            release_turn.set()
            if slot.task is not None:
                try:
                    await slot.task
                except (Exception, asyncio.CancelledError):
                    pass
            rt = slot._regenerate_restore_task
            if rt is not None:
                try:
                    await rt
                except (Exception, asyncio.CancelledError):
                    pass
            await asyncio.sleep(0)

    rows = state.conversation_log.read_messages(original_key)
    original_copies = [m for m in rows if m.get("content") == "ORIGINAL-REPLY"]
    assert len(original_copies) == 1, (
        "the old reply must be persisted exactly once " f"(found {len(original_copies)})"
    )
    # No recovery write happened while the turn was still in-flight: the drain
    # quiesced the turn before writing anything. Pre-fix (return-early) the drain
    # wrote the pre-turn recovery with the turn still live.
    assert writes_while_turn_live["n"] == 0, (
        "the drain must not write a recovery while the regenerate turn is still in-flight; "
        f"it must quiesce the turn first (writes-while-live={writes_while_turn_live['n']})"
    )
    _cr._PENDING_RECOVERIES.clear()
