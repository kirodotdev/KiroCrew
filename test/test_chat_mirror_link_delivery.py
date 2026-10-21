"""Mirror-link edge cases, backfill fidelity and the inbound claim a link request takes.

Pins what the mirror-link endpoint answers for a malformed body, a reminder that cannot be
posted, a target that cannot be resolved, a failed first send or backfill send, a lost
ownership race, a failing occupancy precheck and governance that narrows before the first
send; the non-Slack backfill (turn-aware selection, chunking, the delivery budget, numbered
options, unconverted markdown, redaction) and the claim ordering and rollback around it;
and the inbound marker on the claim, which follows the transport's resume capability and
target policy, never survives a governance denial and has the occupancy precheck in front of
it.
The request contract, the Discord DM mirror, unlink, reminder mode, pause and the
channel-target listing live in ``test_chat_mirror.py``; the shared fakes and builders live
in ``chat_mirror_helpers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_mirror_helpers import (
    _fake_transport,
    _make_mirror_app,
    _prep,
    _real_caps_transport,
)

from kiro_crew.messaging.link import ChannelLink


class TestMirrorLinkEdgeCases:
    """Cover remaining edge cases in mirror-link: invalid JSON, reminder failure,
    target resolution failure, initial delivery failure, ownership conflict race."""

    @pytest.mark.asyncio
    async def test_invalid_json_body_returns_400(self, tmp_path, monkeypatch):
        """Non-JSON text body returns 400."""
        state = _prep(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                data=b"{invalid json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert "valid JSON" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_reminder_delivery_failure_returns_502(self, tmp_path, monkeypatch):
        """When reminder send fails, 502 is returned."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("discord")
        transport.send_message = AsyncMock(side_effect=RuntimeError("network down"))
        state.register_channel_transport(transport)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("discord", channel_id="chan1")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 502
            assert "failed to post reminder" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_reminder_mirror_not_live_returns_503(self, tmp_path, monkeypatch):
        """When mirror target cannot be resolved but link exists, 503."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=False),
        )
        state = _prep(tmp_path, monkeypatch)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="789")
        )
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/mirror-link")
            assert resp.status == 503
            assert "not live" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_configured_target_unavailable_returns_409(self, tmp_path, monkeypatch):
        """When resolve_configured_target returns None, 409 is returned."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.resolve_configured_target = AsyncMock(return_value=None)
        state.register_channel_transport(transport)
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:bad"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "configured_target_unavailable"

    @pytest.mark.asyncio
    async def test_initial_delivery_failure_returns_502(self, tmp_path, monkeypatch):
        """When the initial link message send fails, 502 with channel_link_failed."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        transport.resolve_configured_target = AsyncMock(return_value=("conv1", None))
        transport.send_message = AsyncMock(side_effect=RuntimeError("timeout"))
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "channel_link_failed"

    @pytest.mark.asyncio
    async def test_ownership_conflict_race_returns_409(self, tmp_path, monkeypatch):
        """ConversationOwnershipConflict during set_mirror_link returns 409."""
        from kiro_crew.session_map import ConversationOwnershipConflict

        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_opt_out = MagicMock()
        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("race")
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"

    @pytest.mark.asyncio
    async def test_occupancy_precheck_exception_degrades_open(self, tmp_path, monkeypatch):
        """An exception in mirror_claim_blockers degrades open (allows link)."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(
            side_effect=RuntimeError("accessor unavailable")
        )
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_opt_out = MagicMock()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

        # Link was still persisted despite the precheck exception.
        state.sessions.set_mirror_link.assert_called_once()

    @pytest.mark.asyncio
    async def test_governance_narrows_between_resolution_and_initial_send(
        self, tmp_path, monkeypatch
    ):
        """Governance denying at the send-boundary recheck returns 403.

        This is distinct from the pre-resolution denial: target resolution
        succeeds, but the recheck at the actual send boundary (line 313-316)
        finds that governance narrowed while the resolution yielded.
        """
        call_count = {"n": 0}

        def _permits(*args, **kwargs):
            call_count["n"] += 1
            # First call passes (the pre-resolution governance), second denies
            # (the send-boundary recheck).
            return SimpleNamespace(permitted=call_count["n"] <= 1)

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _permits)
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_permitted"

        # No binding may SURVIVE. The claim is taken before the announcement, so
        # this denial releases it rather than never writing it.
        assert state.sessions.get_mirror_link("dashboard:s1") is None
        # The initial announcement must NOT have been sent either.
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_context_delivery_failure_is_silent(self, tmp_path, monkeypatch):
        """A failure during backfill delivery does not break the link."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram")
        call_count = 0

        async def _flaky_send(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Succeed on the initial announcement, fail on backfill delivery.
            if call_count > 1:
                raise RuntimeError("flaky network")
            return "mid-1"

        transport.send_message = _flaky_send
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=[])
        state.sessions.set_mirror_link = MagicMock()
        state.sessions.set_mirror_opt_out = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "test msg"},
                {"role": "assistant", "content": "reply msg"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            # Link still succeeds despite context delivery failure
            assert resp.status == 200

        state.sessions.set_mirror_link.assert_called_once()


class TestMirrorBackfillFidelity:
    """The non-Slack mirror seeds the same turn-aware history, chunked not cut.

    Deliberately asymmetric with the Slack path: this delivery stays INLINE
    because its per-message governance re-check has to be able to fail the
    request closed with 403, which a backgrounded drain could not do after the
    handler had already returned 200 and persisted the link.
    """

    # A LITERAL ceiling, deliberately NOT chat_mirror._MAX_INLINE_BACKFILL_UNITS:
    # asserting against the module constant would move with it, so raising the
    # cap — or deleting it — would still pass. This leaves headroom for a
    # deliberate tuning change while still failing an unbounded loop.
    _BOUND_CEILING = 16

    def _linked(self, tmp_path, monkeypatch, max_message_chars=4096):
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", max_message_chars=max_message_chars)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        return state, transport

    async def _link(self, state):
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200

    def _sent(self, transport):
        """Delivered bodies, excluding the link announcement."""
        texts = [call.args[1] for call in transport.send_message.await_args_list]
        return [t for t in texts if "Session linked from dashboard" not in t]

    @pytest.mark.asyncio
    async def test_filter_runs_before_slice(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for role, content in [
            ("user", "why is the build red"),
            ("assistant", "a lint rule changed"),
            ("tool", "grep ..."),
            ("tool", "cat ..."),
            ("tool", "pytest ..."),
        ]:
            slot.append(role, content)
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "why is the build red" in body
        assert "a lint rule changed" in body
        assert "grep" not in body and "pytest" not in body

    @pytest.mark.asyncio
    async def test_an_options_trailer_is_backfilled_as_numbered_text(self, tmp_path, monkeypatch):
        # send_message takes no choices, so a backfilled answer builds no button:
        # the trailer must arrive as a numbered list, not as raw markup.
        state = _prep(tmp_path, monkeypatch)
        transport = _real_caps_transport("telegram")
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.append("user", "ship it?")
        slot.append("assistant", "Ready.\n\n[OPTIONS: Ship now | Wait]")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "[OPTIONS" not in body
        assert "1. Ship now" in body and "2. Wait" in body

    @pytest.mark.asyncio
    async def test_long_message_is_chunked_not_truncated(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch, max_message_chars=500)
        slot = state.get_or_create_slot("s1")
        long_answer = "".join(f"[{i:04d}]" for i in range(600))  # 3600 chars
        slot.append("user", "explain")
        slot.append("assistant", long_answer)
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        assert all(len(text) <= 500 for text in sent), "a chunk exceeded the transport limit"
        body = "".join(sent)
        for i in (0, 300, 599):
            assert f"[{i:04d}]" in body, f"marker {i} lost — content was truncated"

    @pytest.mark.asyncio
    async def test_first_turn_and_gap_marker(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 11):
            slot.append("user", f"question {i}")
            slot.append("assistant", f"answer {i}")
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        body = "\n".join(sent)
        assert "question 1" in body
        assert "question 10" in body
        assert "question 3" not in body
        markers = [t for t in sent if "earlier turn" in t]
        assert len(markers) == 1
        # Slack would report 4 skipped (10 turns, 5 recent). The inline path is
        # additionally under a delivery budget, so the oldest recent turn is
        # folded into the marker instead of being sent -- 5, not 4. That fold is
        # the point of the budget: the marker absorbs the overflow.
        assert "5 earlier turns" in markers[0]

    @pytest.mark.asyncio
    async def test_history_that_fits_exactly_is_not_trimmed(self, tmp_path, monkeypatch):
        """No gap marker when there is no gap.

        Six two-message turns is exactly the 12-unit budget. An earlier version
        reserved the marker's slot unconditionally, so the reservation pushed the
        oldest turn out and then spent that slot announcing the omission it had
        itself caused — a false gap on history that fit.
        """
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 7):
            slot.append("user", f"q{i}")
            slot.append("assistant", f"a{i}")
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        body = "\n".join(sent)
        assert not any("earlier turn" in t for t in sent), f"false gap marker: {sent}"
        for i in range(1, 7):
            assert f"q{i}" in body, f"turn {i} was trimmed even though it fit"
        assert len(sent) == 12, f"expected all 12 units, got {len(sent)}"

    @pytest.mark.asyncio
    async def test_inline_delivery_is_bounded(self, tmp_path, monkeypatch):
        """The request cannot grow without limit just because history did.

        This path is inline (its governance re-check must be able to 403), so
        every extra unit is another governance hop plus a send on a channel that
        may accept ~1 msg/s. Long history must not push the request past a
        browser fetch timeout.
        """

        state, transport = self._linked(tmp_path, monkeypatch, max_message_chars=200)
        slot = state.get_or_create_slot("s1")
        for i in range(1, 9):
            slot.append("user", f"question {i}")
            slot.append("assistant", f"answer {i} " + "y" * 900)  # ~5 units each
        slot.drain()

        await self._link(state)
        sent = self._sent(transport)
        assert (
            len(sent) <= self._BOUND_CEILING
        ), f"inline delivery sent {len(sent)} units, over the budget"
        # Priority order: the newest turn is irreducible, then the marker, then
        # the opening turn, then older turns. Here each turn costs ~6 units, so
        # the opening turn cannot be afforded and is folded into the count.
        body = "\n".join(sent)
        assert "question 8" in body, "newest turn was trimmed away"
        assert any("earlier turn" in t for t in sent), "trim happened with no marker"

    @pytest.mark.asyncio
    async def test_delivery_scales_with_the_budget_not_with_history(self, tmp_path, monkeypatch):
        """Ten times the history must not mean ten times the request duration."""

        counts = []
        for turn_count in (8, 80):
            state, transport = self._linked(tmp_path, monkeypatch)
            slot = state.get_or_create_slot("s1")
            for i in range(1, turn_count + 1):
                slot.append("user", f"q{i}")
                slot.append("assistant", f"a{i}")
            slot.drain()
            await self._link(state)
            counts.append(len(self._sent(transport)))

        assert all(c <= self._BOUND_CEILING for c in counts), counts
        assert (
            counts[0] == counts[1]
        ), f"unit count tracked history length ({counts}) instead of the budget"

    @pytest.mark.asyncio
    async def test_no_slack_mrkdwn_conversion_on_a_non_slack_channel(self, tmp_path, monkeypatch):
        """Telegram is not Slack: markdown must pass through unconverted."""
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "doc it")
        slot.append("assistant", "## Heading\n\n**bold** text")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "## Heading" in body
        assert "**bold**" in body

    @pytest.mark.asyncio
    async def test_credentials_are_redacted(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        secret = "AKIAIOSFODNN7EXAMPLE"
        slot.append("user", "creds")
        slot.append("assistant", f"key is {secret}")
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert secret not in body

    @pytest.mark.asyncio
    async def test_compaction_rows_are_excluded(self, tmp_path, monkeypatch):
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "real question")
        slot.append("assistant", "real answer")
        slot.append("assistant", "context compacted", meta={"kind": "compaction"})
        slot.drain()

        await self._link(state)
        body = "\n".join(self._sent(transport))
        assert "real question" in body and "real answer" in body
        assert "context compacted" not in body

    @pytest.mark.asyncio
    async def test_delivery_stays_inline_so_the_link_persists_after_seeding(
        self, tmp_path, monkeypatch
    ):
        """The 200 must not be returned before the seeding is delivered.

        This is the property that forbids backgrounding this path. The observable
        is the ORDER of the sends against the response, not the position of the
        persist: the binding is now claimed BEFORE the announcement, because an
        inbound reply arriving during a multi-second backfill would otherwise run
        in the channel's native session.
        """
        state, transport = self._linked(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "one")
        slot.append("assistant", "two")
        slot.drain()

        order: list[str] = []
        original_send = transport.send_message

        async def _tracked_send(*args, **kwargs):
            order.append("send")
            return await original_send(*args, **kwargs)

        transport.send_message = _tracked_send
        real_set = state.sessions.set_mirror_link

        def _tracked_set(*a, **k):
            order.append("persist")
            return real_set(*a, **k)

        state.sessions.set_mirror_link = MagicMock(side_effect=_tracked_set)

        await self._link(state)
        assert "persist" in order, "link was never persisted"
        # Claimed first, so every send follows it — and the request did not return
        # until they had all gone out, which is what "inline" means here.
        assert order.index("persist") == 0, "the claim must precede the announcement"
        assert order.count("send") >= 3, "announcement + both messages should have been sent"

    @pytest.mark.asyncio
    async def test_an_unreadable_opt_out_is_left_alone_on_rollback(self, tmp_path, monkeypatch):
        """A failed READ must not mutate state.

        Defaulting the snapshot to False would let the rollback clear a standing
        refusal to mirror that the user set deliberately — a read failure silently
        changing a preference.
        """
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", session_resume=True)
        state.register_channel_transport(transport)
        state.sessions.mirror_opt_out = MagicMock(side_effect=OSError("unreadable"))
        state.sessions.set_mirror_opt_out = MagicMock()
        transport.send_message = AsyncMock(side_effect=RuntimeError("transport died"))

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 502

        # The claim withdrew it, but the rollback must not GUESS a value to restore.
        restored = [
            call
            for call in state.sessions.set_mirror_opt_out.call_args_list
            if call.args[1] is True
        ]
        assert not restored, "the rollback invented an opt-out value it never read"
        assert state.sessions.get_mirror_link("dashboard:s1") is None

    @pytest.mark.asyncio
    async def test_a_claim_that_cannot_persist_leaves_no_live_binding(self, tmp_path, monkeypatch):
        """A batch writes on the way OUT, so a failed write leaves live-but-undurable.

        The in-memory map is already mutated when the write fails, so without this
        the request 500s while inbound routing resolves a binding that no restart
        can recover.
        """
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", session_resume=True)
        state.register_channel_transport(transport)
        real_batched_save = state.sessions.batched_save
        failed: list[bool] = []

        from contextlib import contextmanager

        @contextmanager
        def _batch_that_fails_on_exit():
            with real_batched_save():
                yield
            if not failed:
                failed.append(True)
                raise OSError("disk full")

        state.sessions.batched_save = _batch_that_fails_on_exit

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "channel_link_failed"

        assert failed, "the write failure was never simulated"
        assert state.sessions.get_mirror_link("dashboard:s1") is None
        assert transport.send_message.await_count == 0, "nothing may be announced"

    @pytest.mark.asyncio
    async def test_a_refused_claim_does_not_withdraw_the_opt_out(self, tmp_path, monkeypatch):
        """A conflict must leave no trace, including the standing mirror refusal.

        ``set_mirror_link`` refuses before mutating, so ordering it ahead of the
        opt-out withdrawal is what keeps a refused link from flipping a preference
        the user set deliberately.
        """
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", session_resume=True)
        state.register_channel_transport(transport)

        from kiro_crew.session_map import ConversationOwnershipConflict

        state.sessions.set_mirror_link = MagicMock(
            side_effect=ConversationOwnershipConflict("taken")
        )
        state.sessions.set_mirror_opt_out = MagicMock()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"

        state.sessions.set_mirror_opt_out.assert_not_called()
        assert transport.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_a_rebind_during_delivery_survives_a_failed_link(self, tmp_path, monkeypatch):
        """The rollback must not restore stale state over a newer binding.

        Claiming first means a failure has something to undo — but between the
        claim and the failure another writer may have rebound the session, and it
        takes no lock of ours. The undo is therefore conditional: it fires only
        while the binding is still this request's own claim.
        """
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", session_resume=True)
        state.register_channel_transport(transport)
        rival = ChannelLink(channel_type="telegram", channel_id="999", thread_id=None)
        at_send: list[Any] = []

        async def _rebind_then_fail(*args, **kwargs):
            # What the claim looks like at the first send — this is the invariant
            # the whole reorder exists for.
            at_send.append(state.sessions.get_mirror_link("dashboard:s1"))
            # A concurrent writer takes the session while the announcement is in
            # flight, then this delivery fails.
            state.sessions.set_mirror_link("dashboard:s1", rival, accepts_inbound=True)
            raise RuntimeError("transport died")

        transport.send_message = _rebind_then_fail

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 502

        assert at_send, "the announcement was never attempted"
        assert at_send[0] is not None, "the claim was not in place before the first send"
        observed = state.sessions.get_mirror_link("dashboard:s1")
        assert observed == rival, f"rollback overwrote a newer binding; observed {observed!r}"

    @pytest.mark.asyncio
    async def test_a_failed_link_releases_its_own_claim(self, tmp_path, monkeypatch):
        """Non-vacuity: with no rival, the claim really is released."""
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", session_resume=True)
        state.register_channel_transport(transport)
        transport.send_message = AsyncMock(side_effect=RuntimeError("transport died"))

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 502

        assert state.sessions.get_mirror_link("dashboard:s1") is None

    @pytest.mark.asyncio
    async def test_a_reply_during_the_backfill_resolves_the_dashboard_session(
        self, tmp_path, monkeypatch
    ):
        """The window this ordering exists to close.

        The notice says "continuing here", then the catch-up transcript goes out
        one message at a time against the transport's rate limit — seconds, not an
        instant. A reply arriving in that window must already resolve THIS session;
        with the claim taken last it resolved nothing and ran in the channel's
        native session, which is the one the notice said the user had left.
        """
        state = _prep(tmp_path, monkeypatch)
        transport = _fake_transport("telegram", session_resume=True)
        state.register_channel_transport(transport)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "one")
        slot.append("assistant", "two")
        slot.drain()

        link = ChannelLink(channel_type="telegram", channel_id="123", thread_id=None)
        resolved_mid_delivery: list[list[str]] = []
        original_send = transport.send_message

        async def _probe_send(*args, **kwargs):
            # Asked at every send, INCLUDING the first (the announcement), which is
            # the earliest moment a user could possibly reply.
            resolved_mid_delivery.append(
                state.sessions.find_mirror_sessions(link, inbound_only=True)
            )
            return await original_send(*args, **kwargs)

        transport.send_message = _probe_send

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200

        assert resolved_mid_delivery, "nothing was delivered, so the window was never observed"
        assert all(
            owners == ["dashboard:s1"] for owners in resolved_mid_delivery
        ), f"the binding was not resolvable during delivery: {resolved_mid_delivery}"


class TestInboundClaimFollowsTheCapability:
    """Connecting claims INBOUND only where the transport's inbound path honours it.

    This is the fix for the reported bug. Without the claim the connect writes an
    outbound-only binding, the channel's inbound resolver skips it, and the user's
    reply starts a brand-new session with none of this transcript.
    """

    @pytest.mark.asyncio
    async def test_a_resume_capable_transport_gets_an_inbound_binding(self, tmp_path, monkeypatch):
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord", session_resume=True))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is True

    @pytest.mark.asyncio
    async def test_target_policy_can_keep_capable_transport_outbound_only(
        self, tmp_path, monkeypatch
    ):
        transport = _fake_transport("telegram", session_resume=True)
        transport.may_resume_from = MagicMock(return_value=False)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.set_mirror_link = MagicMock()

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "telegram", "target_id": "user:123"},
            )
            assert resp.status == 200

        transport.may_resume_from.assert_called_once_with("123", None)
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is False

    @pytest.mark.asyncio
    async def test_a_transport_that_cannot_resume_stays_outbound_only(self, tmp_path, monkeypatch):
        """A transport without an inbound resolver must never receive the marker."""
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("synthetic", session_resume=False))
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "synthetic", "target_id": "user:123"},
            )
            assert resp.status == 200
        assert state.sessions.set_mirror_link.call_args.kwargs["accepts_inbound"] is False

    @pytest.mark.asyncio
    async def test_an_occupied_conversation_is_refused_before_anything_is_posted(
        self, tmp_path, monkeypatch
    ):
        """A binding can be unwound; posted messages cannot.

        The authoritative check is atomic inside ``set_mirror_link``, but that
        fires only after the link notice and the whole catch-up transcript have
        been delivered. So the same question is asked at the first point the real
        location is known, and nothing is sent into a conversation this session
        does not get to own.
        """
        transport = _fake_transport("discord", session_resume=True)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        state.sessions.mirror_claim_blockers = MagicMock(return_value=["dashboard:someone-else"])
        state.sessions.set_mirror_link = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "private"},
                {"role": "assistant", "content": "transcript"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "conversation_occupied"

        assert (
            transport.send_message.await_count == 0
        ), "the transcript was delivered into a conversation another session owns"
        state.sessions.set_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stubbed_session_manager_does_not_read_as_occupied(self, tmp_path, monkeypatch):
        """A Mock is truthy, and truthy must not mean "taken".

        Read as a rival list, a stubbed accessor's Mock would refuse every connect
        and the refusal would look like a real conflict. Only an actual list is an
        answer; anything else means "no precheck", and the writer still enforces.
        """
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(_fake_transport("discord", session_resume=True))
        state.sessions.mirror_claim_blockers = MagicMock(return_value=MagicMock())
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_governance_denial_persists_no_inbound_binding(self, tmp_path, monkeypatch):
        """The write that grants inbound capability must stay behind the gate.

        Persisting is the side effect that matters: a binding written for a
        message governance went on to deny would leave the conversation connected
        AND inbound-capable, so the channel would keep resuming a session policy
        had just refused. The endpoint's existing fail-closed path is what
        prevents it — this pins that the strengthened write inherits it.
        """
        transport = _fake_transport("discord", session_resume=True)

        def _permits(*args, **kwargs):
            # Keyed on "has anything been delivered yet?", not a call count, so
            # the denial lands on the first in-loop unit regardless of how many
            # messages the backfill selects.
            return SimpleNamespace(
                permitted=not transport.send_message.await_args_list,
                rule="",
                layer="",
                reason="",
            )

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _permits)
        state = _prep(tmp_path, monkeypatch)
        state.register_channel_transport(transport)
        slot = state.get_or_create_slot("s1")
        slot.messages.extend(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        )

        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 403

        # The claim is taken before delivery, so the guarantee is that no binding —
        # and in particular no INBOUND one — survives the denial.
        assert state.sessions.get_mirror_link("dashboard:s1") is None
        link = ChannelLink(channel_type="discord", channel_id="123", thread_id=None)
        assert state.sessions.find_mirror_sessions(link, inbound_only=True) == []

    @pytest.mark.asyncio
    async def test_the_precheck_asks_the_writers_exact_question(self, tmp_path, monkeypatch):
        """The precheck must pass the claim's inbound intent, not just the location.

        Drop the argument and the precheck answers a different question from the
        writer it backs -- refusing where the writer allows, which would newly
        reject a second outbound-only mirror on transports that cannot resume at
        all. The sentinel default is what makes omission detectable: a plain
        ``False`` default would make "passed False" and "not passed" identical, so
        the test would pass against the very divergence it exists to catch.
        """
        sentinel = object()
        seen: list[object] = []

        def _blockers(key, link, *, accepts_inbound=sentinel):
            seen.append(accepts_inbound)
            return []

        state = _prep(tmp_path, monkeypatch)
        # Discord declares session resume, so a threaded argument is True here and
        # an omitted one would fall to the default -- two distinguishable states.
        state.register_channel_transport(_fake_transport("discord", session_resume=True))
        state.sessions.mirror_claim_blockers = _blockers
        state.sessions.set_mirror_link = MagicMock()
        async with TestClient(TestServer(_make_mirror_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/mirror-link",
                json={"channel_type": "discord", "target_id": "user:123"},
            )
            assert resp.status == 200
        assert seen == [True], (
            f"the precheck did not ask the writer's question with this claim's "
            f"inbound intent: {seen}"
        )
