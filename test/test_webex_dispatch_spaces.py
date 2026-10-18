"""WebexDispatcher in group spaces: which session a space turn runs in, and a drain that answers
one member of a shared space.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest
from webex_dispatch_helpers import (
    _EMAIL,
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _cfg_group,
    _dispatcher,
    _inbound,
    _space,
    _spin,
)

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
from kiro_crew.webex import transport_dispatch as webex_dispatch

# ------------------------------------------------------------------
# Tests: group spaces
# ------------------------------------------------------------------


class TestGroupSpaceRouting:
    @pytest.mark.asyncio
    async def test_a_space_turn_does_not_run_in_the_senders_dm(self) -> None:
        """The single most consequential routing property on this channel.

        A space keyed by the sender answers their PRIVATE history into a room, is
        the target a mid-turn DM steers into, and is what their `/new` resets.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=_cfg_group())
        keys: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            keys.append(turn.session_key)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_space("hi"))
            await d.handle_message(_inbound("hi"))

        space_key, dm_key = keys
        assert space_key != dm_key
        assert "SPACE1" in space_key
        assert _EMAIL not in space_key

    @pytest.mark.asyncio
    async def test_a_space_is_namespaced_as_a_forum_so_unified_cannot_collapse_it(self) -> None:
        """``dm_scope=unified`` collapses DIRECT DMs into one cross-surface bucket.

        A shared space merging into one person's private bucket would answer their
        DM history into the room, so the space is namespaced `forum`, which that
        collapse skips.
        """
        cfg = _cfg_group()
        cfg.messaging.dm_scope = "unified"
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient(), cfg=cfg)
        keys: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            keys.append(turn.session_key)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_space("hi"))
            await d.handle_message(_inbound("hi"))

        space_key, dm_key = keys
        assert dm_key.startswith("unified:")
        assert not space_key.startswith("unified:")
        assert ":forum:" in space_key

    @pytest.mark.asyncio
    async def test_new_in_a_space_does_not_reset_the_senders_dm(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client, cfg=_cfg_group())
        dm_before = d._session_key(_EMAIL)

        with mock.patch.object(type(d), "_bot_name", lambda self: "Kiro"):
            await d.handle_message(_space("Kiro /new"))

        assert "fresh conversation" in client.sent[-1][1]
        assert d._session_key(_EMAIL) == dm_before

    @pytest.mark.asyncio
    async def test_a_mention_is_stripped_before_the_command_and_approval_paths(self) -> None:
        """In a space Webex delivers only @mentions and does NOT strip the name.

        So every path that READS the text has to run after the strip: "@Kiro 1" is
        an approval answer and "@Kiro /new" is a command, and matching either
        against the raw text fails in exactly the room where the mention is
        mandatory.
        """
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg_group())
        with mock.patch.object(type(d), "_bot_name", lambda self: "Kiro"):
            key = d._session_key(webex_dispatch._route_of(_space()))
            task = asyncio.create_task(
                webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
            )
            await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
            await d.handle_message(_space("Kiro 1"))

        # flake-ok: pre-existing await on a decision task the step above resolves, moved verbatim
        assert (await task)[0] is True
        assert provider.steered == []

    @pytest.mark.asyncio
    async def test_uploads_are_off_in_a_space(self) -> None:
        """A file posted into a space is readable by every member of it, including
        people the email allow-list excludes."""
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient(), cfg=_cfg_group())
        seen: list = []

        real = webex_dispatch.WebexRenderer

        def _spy(*a, **kw):
            seen.append(kw.get("uploads_allowed"))
            return real(*a, **kw)

        async def _noop(turn, *, sessions, ctx_builder):
            pass

        with mock.patch("kiro_crew.webex.transport_dispatch.WebexRenderer", _spy):
            with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _noop):
                await d.handle_message(_space("hi"))
                await d.handle_message(_inbound("hi"))

        assert seen == [False, True]

    @pytest.mark.asyncio
    async def test_a_space_turn_is_built_without_the_operators_private_context(self) -> None:
        """A space is a shared audience the allow-list does not cover.

        The operator's memory, lessons, skills and history are injected into the
        PROMPT before any tool runs, so a full-context turn could quote the
        operator's private notes into a room they do not control. A space turn is
        built with ``minimal_context``; a direct DM keeps the full context.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient(), cfg=_cfg_group())
        seen: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            seen.append(turn.minimal_context)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_space("hi"))
            await d.handle_message(_inbound("hi"))

        assert seen == [True, False]


class TestAPartialDrainInASharedSpace:
    """Two members of one space, one bubble, and a drain that answers ONE of them.

    ``_route_of`` maps a space to ``space:{room_id}``, so both members share a session
    key AND a receipt address: their messages are listed on the single bubble, and the
    drain still collapses only the entries whose sender matches. So the drain that
    answers the first member meets a bubble that is still listing the second member's
    queued message -- and that entry is the only handle it has. Retiring it flipped the
    bubble to "Now answering (1)" over a list of two, erasing an acknowledgement for a
    message that was still queued, and the second member's own drain then found no entry
    and recorded them nowhere at all.

    Driven through the real producer and the real pump rather than the registry alone,
    because the halves live in two modules: which entries collapse is decided here in the
    transport, and what the bubble does about the remainder is decided in
    ``messaging/queue_receipt.py``.
    """

    OTHER = "other@example.com"

    def _dispatcher_and_key(self, client: FakeClient, sessions: FakeSessions):
        cfg = _cfg_group()
        cfg.messaging.queue_mode = "queue"
        cfg.webex.allowed_emails = [_EMAIL, self.OTHER]
        d = _dispatcher(sessions, FakeCtx(), client, cfg=cfg)
        return d, d._session_key(webex_dispatch._route_of(_space(email=_EMAIL)))

    @staticmethod
    def _receipt_bodies(client: FakeClient) -> list[str]:
        """Every body the receipt bubble was rewritten to, in order."""
        return [body for _mid, _room, body in client.edits]

    @pytest.mark.asyncio
    async def test_the_bubble_keeps_the_second_members_line_and_its_handle(self) -> None:
        client, sessions = FakeClient(), FakeSessions(FakeProvider([]))
        d, key = self._dispatcher_and_key(client, sessions)
        sessions._busy = True
        await d._enqueue_with_receipt(key, "kyle asked", _space("kyle asked", email=_EMAIL))
        await d._enqueue_with_receipt(key, "other asked", _space("other asked", email=self.OTHER))
        sessions._busy = False
        seen: list[tuple[str, str]] = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.person_email, inbound.text))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(key, _space("kyle asked", email=_EMAIL))

        bodies = self._receipt_bodies(client)
        assert seen == [(_EMAIL, "kyle asked"), (self.OTHER, "other asked")]
        # One bubble for the space, never a second one opened beside a stale one.
        assert len([body for _room, body in client.sent if "Queued" in body]) == 1
        # The partial drain re-rendered it as STILL QUEUED over what is still queued.
        partial = bodies[1]
        assert "Queued (1)" in partial and "other asked" in partial
        assert "kyle asked" not in partial, "the answered line left the queued list"
        # The second member's own drain then flips it, and that is the only record.
        answering = [body for body in bodies if "Now answering" in body]
        assert answering == [bodies[-1]]
        assert "other asked" in answering[0] and "kyle asked" not in answering[0]
        assert not d._queue.has_receipt(key), "nothing is left queued, so the key is released"

    @pytest.mark.asyncio
    async def test_the_second_member_is_recorded_exactly_once(self) -> None:
        """No duplicate record, and no record naming the wrong member's text.

        The failure this pins is subtler than a missing record: a retained entry that a
        later transition mis-reads writes the record TWICE, or writes the first member's
        text into the record the second member reads.
        """
        client, sessions = FakeClient(), FakeSessions(FakeProvider([]))
        d, key = self._dispatcher_and_key(client, sessions)
        sessions._busy = True
        await d._enqueue_with_receipt(key, "kyle asked", _space("kyle asked", email=_EMAIL))
        await d._enqueue_with_receipt(key, "other asked", _space("other asked", email=self.OTHER))
        sessions._busy = False

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            return None

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(key, _space("kyle asked", email=_EMAIL))

        posted = [body for _room, body in client.sent]
        bodies = self._receipt_bodies(client)
        assert sum("Now answering" in body for body in bodies) == 1
        assert not any(
            "Now answering" in body for body in posted
        ), "the bubble was editable throughout, so no record needed posting beside it"
        assert (
            sum(body.count("other asked") for body in bodies) == 3
        ), "listed on the grow, on the partial re-render, and on its own record"

    @pytest.mark.asyncio
    async def test_a_second_thread_in_the_space_does_not_hold_this_bubble(self) -> None:
        """The other side: a line at another ADDRESS was never rendered here.

        With ``reply_in_thread`` on, two threads of one space are two receipt addresses
        while still sharing the session key. The sibling thread's line is recorded on the
        entry but shown on no bubble, so it is owed nothing by this one -- which must
        therefore retire normally rather than waiting for a message it never listed.
        """
        client, sessions = FakeClient(), FakeSessions(FakeProvider([]))
        d, key = self._dispatcher_and_key(client, sessions)
        sessions._busy = True
        here = _space("kyle asked", email=_EMAIL)
        sibling = replace(_space("other asked", email=self.OTHER), parent_id="THREAD_B")
        await d._enqueue_with_receipt(key, "kyle asked", here)
        await d._enqueue_with_receipt(key, "other asked", sibling)
        sessions._busy = False

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            return None

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(key, here)

        bodies = self._receipt_bodies(client)
        assert any("Now answering" in body and "kyle asked" in body for body in bodies)
        assert all(
            "other asked" not in body for body in bodies
        ), "a sibling thread's text is never rendered on this thread's bubble"
