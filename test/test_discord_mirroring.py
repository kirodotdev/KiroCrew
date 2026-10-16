"""A Discord conversation is its own mirror, and its turn opens the conversation's crew log.

A turn binds the conversation (or its thread) as the session's mirror unless
the user unlinked it, the conversation is ambiguous, the session was resumed
from elsewhere, a unified DM scope is in force, or an existing binding aims the
session at another channel. ``!link`` and ``!unlink`` free and claim one
location in one write. A direct-message turn opens the crew log the work ledger writes
into, at the allocation, citing the predecessor of a recycled conversation.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest
from crew_log_drain import assert_drained, settle, unsync_appends
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeProvider, FakeSessions, _dispatcher
from off_loop_helpers import off_loop

from kiro_crew.messaging.link import ChannelLink, legacy_dashboard_mirror_key
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import _opt_out_key


def _read_session_log(unit: str) -> list[Any]:
    """Every entry of *unit*'s session crew log, oldest first.

    Takes the unit's append lock, so a coroutine calls it through ``off_loop``:
    the eager folder reads the unit on its own thread after each entry, and an
    acquire on the event-loop thread makes one attempt that such a read refuses.
    """
    from kiro_crew.crew_log import projection

    handle = projection.open_session_log(unit)
    assert handle is not None, f"no crew log for {unit!r}"
    return list(handle.iter_from(1, known=projection.KNOWN_TYPES))


class TestDispatcherOriginMirroring:
    def _msg(self, text: str, user: str = "u1", chan: str = "c1") -> InboundMessage:
        return InboundMessage(channel_type="discord", user_id=user, conversation_id=chan, text=text)

    # ── Automatic origin mirroring ────────────────────────────────────────
    #
    # A Discord conversation IS its own mirror. Without the per-turn bind the
    # binding existed only after an explicit `!link`, so a turn later taken from
    # the dashboard resolved no `discord` target and the chat sat there looking
    # dead while the conversation continued elsewhere.

    @pytest.mark.asyncio
    async def test_a_turn_binds_this_conversation_as_its_own_mirror(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        key = d._session_key("u1")
        assert sess.mirror_links[key] == ChannelLink("discord", channel_id="c1", thread_id=None)

    @pytest.mark.asyncio
    async def test_a_thread_turn_binds_the_thread_channel(self) -> None:
        # A Discord thread IS a channel with its own id, so channel_id already
        # scopes the conversation and is also where the transport posts.
        d, _cli, sess = _dispatcher({"u1"}, allowed_threads={"t9"})
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="t9",
                text="hello",
                thread_id="t9",
            )
        )
        key = d._session_key("u1", "t9")
        assert sess.mirror_links[key] == ChannelLink("discord", channel_id="t9", thread_id=None)

    @pytest.mark.asyncio
    async def test_the_second_turn_writes_nothing(self) -> None:
        # The bind is re-asserted per turn, so the repeating path must be a READ:
        # a session-map mutation rewrites the whole map on the event loop.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("first"))
        writes = len(sess.batched_writes)
        await d.handle_message(self._msg("second"))
        assert len(sess.batched_writes) == writes

    @pytest.mark.asyncio
    async def test_unlink_survives_the_users_next_message(self) -> None:
        # The whole point of persisting the refusal: an entry with no binding is
        # indistinguishable from one that was never linked, so without the flag
        # "off" would last exactly one message.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        await d.handle_message(self._msg("!unlink"))
        await d.handle_message(self._msg("hello again"))
        assert sess.mirror_links == {}

    @pytest.mark.asyncio
    async def test_unlink_survives_a_generation_rotation(self) -> None:
        # `!new` (and the configured idle/daily reset) rotate the :genN suffix.
        # Keyed per generation the refusal would expire on rotation, so an idle
        # reset would undo the user's `!unlink` with no action on their part.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!unlink"))
        await d.handle_message(self._msg("!new"))
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links == {}

    @pytest.mark.asyncio
    async def test_link_withdraws_the_refusal_so_the_bind_resumes(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!unlink"))
        await d.handle_message(self._msg("!link"))
        assert sess.mirror_opt_outs == set()
        sess.mirror_links.clear()  # simulate a sweep / restart-cold binding
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links[d._session_key("u1")].channel_id == "c1"

    @pytest.mark.asyncio
    async def test_link_and_unlink_each_cost_one_batched_write(self) -> None:
        # One user-visible action, one whole-map write — each mutation would
        # otherwise rewrite the entire session map on the loop.
        d, _cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!link"))
        assert sess.batched_writes and all(sess.batched_writes)
        sess.batched_writes.clear()
        await d.handle_message(self._msg("!unlink"))
        assert sess.batched_writes and all(sess.batched_writes)

    @pytest.mark.asyncio
    async def test_a_refused_link_persists_nothing(self) -> None:
        """Ordering guard inside the batch.

        ``batched_save`` writes on the way out even when the block raises, so a
        refusal raised AFTER the opt-out withdrawal would persist that withdrawal
        for a link that never happened — silently turning mirroring back on. The
        claim is refused before it mutates anything, so it goes first. Two owners now
        also make the routing decision refuse `!link` ahead of the handler; either
        way nothing is persisted for a link that did not happen.
        """
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!unlink"))
        self._occupy_ambiguously(sess)
        await d.handle_message(self._msg("!link"))
        assert any("`!unlink`" in t for t, _ in cli.sent)
        assert sess.mirror_opt_outs == {
            _opt_out_key(d._session_key("u1"))
        }, "a refused link must not withdraw the refusal"

    @pytest.mark.asyncio
    async def test_an_ambiguous_conversation_is_answered_but_not_processed(self) -> None:
        """Two owners deny routing, so the turn must be refused — and answered.

        Falling through to this conversation's own session would answer from a
        session holding none of the context the user is looking at; an uncaught
        raise here would answer nothing at all. So: a reply, and no turn.
        """
        d, cli, sess = _dispatcher({"u1"})
        self._occupy_ambiguously(sess)
        await d.handle_message(self._msg("hello world"))
        assert "Ambiguous link" in (cli.final_text() or "")
        assert "Answer: hello world" not in (
            cli.final_text() or ""
        ), "the message was processed while routing was denied"
        assert d._session_key("u1") not in sess.mirror_links

    @pytest.mark.asyncio
    async def test_a_unified_dm_scope_is_not_auto_bound(self) -> None:
        # dm_scope=unified collapses every allowed user's DMs into one
        # unified:{agent} bucket — channel and user drop out of the key — so an
        # automatic bind would deliver one user's dashboard replies into another
        # user's chat. `!link` stays available: it names the channel the user is in.
        # The ORIGIN record is the sibling write and answers to the same rule: an
        # origin naming whoever wrote last would aim unattended output (the
        # auto-compact notice) at that person regardless of whose turn produced it.
        d, _cli, sess = _dispatcher({"u1", "u2"}, dm_scope="unified")
        await d.handle_message(self._msg("hello", user="u1"))
        assert sess.mirror_links == {}
        assert sess.origin_links == {}

    @pytest.mark.asyncio
    async def test_a_dm_turn_opens_the_crew_log_the_work_ledger_writes_into(
        self, monkeypatch, tmp_path
    ) -> None:
        """The work ledger is a projection of the crew log: every write appends a
        ``work/recorded`` entry to the ACTING session's log and rolls the cache back
        (``crew_log_unrecorded``) when there is nowhere to append. A DM that session
        control admits as a conductor therefore needs its log to exist before its
        first ledger call, and only the turn path can create it -- the dashboard
        runner does so on every turn, and this dispatcher runs its own turn loop.

        Real emitter, real writer, isolated home. The admission itself is another
        suite's subject (``test_session_control_owner_dm.py``) and is granted here.
        """
        import json

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log.resolve import unit_for_session_key
        from kiro_crew.dashboard.handlers import work_ledger as ledger_routes

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        unsync_appends(monkeypatch)  # the records are the subject, not their durability
        monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        monkeypatch.setattr(FakeProvider, "session_id", "acp-owner-dm-turn", raising=False)
        monkeypatch.setattr(FakeProvider, "served_model", "model-x", raising=False)
        ledger_routes._BOARD_LOCKS._locks.clear()

        async def _recognized(*a: Any, **k: Any) -> None:
            return None

        monkeypatch.setattr(ledger_routes, "_recognize_session", _recognized)
        monkeypatch.setattr(ledger_routes, "_is_restricted_session", lambda *a: False)
        monkeypatch.setattr(ledger_routes, "contained_channel_caller", lambda state, sk: "")
        emit.reset_caches()
        try:
            d, _cli, sess = _dispatcher({"u1"})
            d.ctx_builder.live_memory_mode_for_session = lambda key: "persistent"
            # The route's own ``!model`` choice: what the allocation is ASKED for,
            # which is not what the provider reports it serves.
            d._model_pref[d._scope_id("u1", "")] = "model-requested"
            await d.handle_message(self._msg("hello"))
            key = d._session_key("u1", "")
            unit = unit_for_session_key(sess, key)
            assert unit == "acp-owner-dm-turn"

            app = web.Application()
            state = mock.MagicMock()
            state.sessions = sess
            app["state"] = state
            req = make_mocked_request(
                "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": key}
            )
            req["internal_auth"] = True
            req.json = mock.AsyncMock(  # type: ignore[method-assign]
                return_value={"action": "goal", "goal": "ship it", "round": 1}
            )
            resp = await ledger_routes.api_work_ledger_record(req)
            body = json.loads(resp.text)
            assert (resp.status, body.get("code")) == (200, None), body

            entries = off_loop(_read_session_log, unit)
            opened = [e for e in entries if e.type == "session/opened"]
            assert len(opened) == 1
            assert (opened[0].data["slot"], opened[0].data["agent"]) == (
                key.replace(":", "_"),
                "kirocrew",
            )
            assert opened[0].data["model"] == "model-x"
            assert opened[0].data.get("model_requested") == "model-requested"
            assert opened[0].data["class"] == {"memory": "persistent", "channel": True}
            assert "parent" not in opened[0].data
            assert [e.type for e in entries].count("work/recorded") == 1
        finally:
            ledger_routes._BOARD_LOCKS._locks.clear()
            await asyncio.to_thread(settle)

    @pytest.mark.asyncio
    async def test_a_tab_on_the_conversation_and_the_channel_state_one_class(
        self, monkeypatch, tmp_path
    ) -> None:
        """A channel conversation with a dashboard tab has TWO writers of one crew log:
        this dispatcher and the dashboard runner, which states the slot's workspace
        beside memory, app and channel. The emitter appends ``session/class`` whenever
        the class it is handed differs from what the log last stated, so an opener
        stating every member but the workspace would have the two writers record a
        move on every switch between them -- a move that never happened. Both read
        the slot the dashboard surfaces the conversation under, so a tab turn between
        two channel turns leaves the log with its opening entry and no ``session/class``.

        Real emitter, real writer, isolated home. The dashboard runner's write is its
        own ``on_session_opened`` for the surfaced slot, carrying the class
        ``_crew_log_class`` and ``_crew_log_workspace`` state for it.
        """
        from types import SimpleNamespace

        from kiro_crew.crew_log import emit
        from kiro_crew.dashboard.channel_slots import channel_slot_name

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        unsync_appends(monkeypatch)  # the records are the subject, not their durability
        monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        monkeypatch.setattr(FakeProvider, "session_id", "acp-tabbed-dm", raising=False)
        emit.reset_caches()
        try:
            d, _cli, _sess = _dispatcher({"u1"})
            d.ctx_builder.live_memory_mode_for_session = lambda key: "persistent"
            key = d._session_key("u1", "")
            # The slot the dashboard surfaces this conversation under, stating the
            # workspace a live slot always states (its constructor default).
            slots = {channel_slot_name(key): SimpleNamespace(workspace="default")}
            d._session_resume.dashboard_state = SimpleNamespace(get_slot=slots.get)
            await d.handle_message(self._msg("hello"))
            # The tab's turn: the dashboard runner opens the same log from the slot.
            emit.on_session_opened(
                "acp-tabbed-dm",
                agent="kirocrew",
                slot=channel_slot_name(key),
                memory="persistent",
                channel=True,
                workspace="default",
            )
            await d.handle_message(self._msg("hello again"))
            await asyncio.to_thread(assert_drained)
            entries = off_loop(_read_session_log, "acp-tabbed-dm")
            assert [e.type for e in entries if e.type == "session/class"] == []
            opened = [e for e in entries if e.type == "session/opened"]
            assert len(opened) == 1
            assert opened[0].data["class"] == {
                "memory": "persistent",
                "channel": True,
                "workspace": "default",
            }
        finally:
            await asyncio.to_thread(settle)

    @pytest.mark.asyncio
    async def test_a_resumed_dashboard_session_is_not_opened_by_the_channel(
        self, monkeypatch
    ) -> None:
        """A dashboard session resumed into the chat is opened by the dashboard
        runner, which alone holds its lineage (``_created_by``); an opener from here
        would create that log without its ``parent``. The channel's own session is
        opened, the resumed one is left to its owner."""
        from kiro_crew.crew_log import emit as crew_log_emit

        opened: list[str] = []
        monkeypatch.setattr(
            crew_log_emit,
            "on_session_opened",
            lambda session_id, **kw: opened.append(session_id),
        )
        monkeypatch.setattr(FakeProvider, "session_id", "acp-any", raising=False)
        d, _cli, _sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("hello"))
        assert opened == ["acp-any"]

        opened.clear()
        d, cli, sess = _dispatcher({"u1"})
        resumed = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:chat-7"] = resumed
        sess.inbound_mirror_keys.add("dashboard:chat-7")
        await d.handle_message(self._msg("hello world"))
        assert "Answer: hello world" in (cli.final_text() or ""), "the resumed turn ran"
        assert sess.origin_links == {}, "the own-session branch was not taken"
        assert opened == []

        # A same-DM NATIVE history picked through ``!sessions`` is a channel session
        # whose turns nobody else runs: it is opened here, under ITS key. A fresh
        # conversation id, because the routing expectation recorded above for
        # ``c1`` names the dashboard session and would refuse a different binding.
        opened.clear()
        d, cli, sess = _dispatcher({"u1"})
        natural = d._session_key("u1", "")
        older = natural.rsplit(":gen", 1)[0] + ":gen0"
        assert older != natural
        sess.mirror_links[older] = ChannelLink("discord", channel_id="c9")
        sess.inbound_mirror_keys.add(older)
        await d.handle_message(self._msg("hello again", chan="c9"))
        assert "Answer: hello again" in (cli.final_text() or ""), "the resumed turn ran"
        assert sess.origin_links == {}, "the resumed branch was taken"
        assert opened == ["acp-any"]

    @pytest.mark.asyncio
    async def test_a_recycled_conversation_opens_its_successor_log_citing_the_predecessor(
        self, monkeypatch, tmp_path
    ) -> None:
        """A failed auto-compaction recycles the session: the pointer in the
        slot-to-session mapping is emptied in place and the next turn cold-starts a
        successor with a new id. Nothing but the ``previous`` edge on the successor's
        ``session/opened`` joins the two crew logs, so without it the conversation's
        earlier history falls off the succession chain. The dashboard runner reads
        ``mapped_sid`` before its allocation for exactly this; the dispatcher must
        read it at the same moment -- the recycle stashes the dropped id and
        ``mapped_sid`` answers from that stash until the successor is mapped.

        Real emitter and writer. The recycle is modelled at its observable seam: the
        mapping still names the predecessor while the provider hands out a new id.
        """
        from kiro_crew.crew_log import emit

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        unsync_appends(monkeypatch)  # the records are the subject, not their durability
        monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        monkeypatch.setattr(FakeProvider, "session_id", "acp-gen-1", raising=False)
        emit.reset_caches()
        try:
            d, _cli, sess = _dispatcher({"u1"})
            d.ctx_builder.live_memory_mode_for_session = lambda key: "persistent"
            await d.handle_message(self._msg("hello"))
            await asyncio.to_thread(assert_drained)

            # The recycle: the mapping keeps answering the dropped id from its stash
            # (``SessionMap.mapped_sid`` reads ``discarded_sid``) while the next
            # allocation cold-starts a successor under a new id.
            sess.mapped_sid = lambda key: "acp-gen-1"
            monkeypatch.setattr(FakeProvider, "session_id", "acp-gen-2", raising=False)
            await d.handle_message(self._msg("and again"))
            await asyncio.to_thread(assert_drained)

            entries = off_loop(_read_session_log, "acp-gen-2")
            opened = [e for e in entries if e.type == "session/opened"]
            assert len(opened) == 1
            assert opened[0].data.get("previous") == {"sid": "acp-gen-1"}
            # A warm turn maps the live id, so the successor's own next turn writes
            # no edge and no second announcement.
            sess.mapped_sid = lambda key: "acp-gen-2"
            await d.handle_message(self._msg("still here"))
            await asyncio.to_thread(assert_drained)
            entries = off_loop(_read_session_log, "acp-gen-2")
            assert [e.type for e in entries].count("session/opened") == 1
        finally:
            await asyncio.to_thread(settle)

    @pytest.mark.asyncio
    async def test_the_predecessor_is_captured_inside_the_allocation_not_around_it(
        self, monkeypatch, tmp_path
    ) -> None:
        """Two cold turns on one conversation race. The loser gets past the busy gate
        and waits INSIDE ``get_or_create`` for the turn permit; meanwhile the winner
        allocates an intermediate session and a failed compaction recycles it, so the
        mapping now names THAT store. A predecessor read anywhere around the call --
        even immediately before it -- predates the intermediate store, so the loser's
        successor cites its grandparent and the intermediate log falls off the chain.
        The store is captured by the allocation boundary inside its own critical
        section and consumed after the claim.

        Modelled at the boundary's seam: the double's ``get_or_create`` suspends
        (the permit wait), the concurrent recycle lands during that suspension, and
        the double captures the predecessor as the real boundary does -- at the
        registration, not before the call."""
        from kiro_crew.crew_log import emit

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        unsync_appends(monkeypatch)  # the records are the subject, not their durability
        monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        emit.reset_caches()
        try:
            d, _cli, sess = _dispatcher({"u1"})
            mapping = {"sid": ""}
            sess.mapped_sid = lambda key: mapping["sid"]
            # Two stores already written by this conversation: the older one, and
            # the intermediate one the winner opened and had recycled.
            for sid in ("acp-gen-0", "acp-gen-1"):
                monkeypatch.setattr(FakeProvider, "session_id", sid, raising=False)
                await d.handle_message(self._msg("hello"))
                mapping["sid"] = sid
            await asyncio.to_thread(assert_drained)

            # The loser's view when it reaches the allocation: the older store.
            mapping["sid"] = "acp-gen-0"
            real_get_or_create = sess.get_or_create

            async def _wait_for_the_permit_then_allocate(key: str, **kwargs: Any) -> Any:
                # Suspended inside the allocation, waiting for the permit. The
                # winner's intermediate session is allocated and recycled meanwhile.
                # flake-ok: the stub's zero-delay yield suspends the allocation; moved verbatim
                await asyncio.sleep(0)
                mapping["sid"] = "acp-gen-1"
                return await real_get_or_create(key, **kwargs)

            sess.get_or_create = _wait_for_the_permit_then_allocate
            monkeypatch.setattr(FakeProvider, "session_id", "acp-gen-2", raising=False)
            await d.handle_message(self._msg("and again"))
            await asyncio.to_thread(assert_drained)

            opened = [
                e for e in off_loop(_read_session_log, "acp-gen-2") if e.type == "session/opened"
            ]
            assert len(opened) == 1
            assert opened[0].data.get("previous") == {"sid": "acp-gen-1"}
        finally:
            await asyncio.to_thread(settle)

    @pytest.mark.asyncio
    async def test_the_log_is_opened_at_the_allocation_even_when_the_turn_then_fails(
        self, monkeypatch, tmp_path
    ) -> None:
        """A recycled conversation's next turn allocates the successor and then fails
        before the turn runs -- here the attachment fetch raises. The allocation
        stands: the successor is live and mapped, so the following turn is a warm
        reuse whose predecessor read names the successor itself. If the log is first
        created THEN, it carries no ``previous`` edge and the predecessor's history is
        detached. So the log is opened the moment the allocation lands, before
        renderer setup, attachment I/O or any other await."""
        import contextlib

        from kiro_crew.crew_log import emit
        from kiro_crew.discord import transport_dispatch as td

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        unsync_appends(monkeypatch)  # the records are the subject, not their durability
        monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        emit.reset_caches()
        try:
            d, _cli, sess = _dispatcher({"u1"})
            monkeypatch.setattr(FakeProvider, "session_id", "acp-gen-0", raising=False)
            await d.handle_message(self._msg("hello"))
            await asyncio.to_thread(assert_drained)

            # The recycle stashed acp-gen-0; the next allocation cold-starts acp-gen-1
            # and the turn then dies in the attachment fetch.
            sess.mapped_sid = lambda key: "acp-gen-0"
            monkeypatch.setattr(FakeProvider, "session_id", "acp-gen-1", raising=False)

            async def _fetch_fails(client: Any, attachments: Any) -> Any:
                raise RuntimeError("attachment fetch failed")

            monkeypatch.setattr(td, "process_discord_attachments", _fetch_fails)
            with contextlib.suppress(Exception):
                await d.handle_message(
                    InboundMessage(
                        channel_type="discord",
                        user_id="u1",
                        conversation_id="c1",
                        text="look at this",
                        attachments=[{"filename": "a.png", "content_type": "image/png"}],
                    )
                )
            await asyncio.to_thread(assert_drained)

            # The successor is live now: the next turn is a warm reuse.
            sess.mapped_sid = lambda key: "acp-gen-1"
            await d.handle_message(self._msg("and again"))
            await asyncio.to_thread(assert_drained)

            opened = [
                e for e in off_loop(_read_session_log, "acp-gen-1") if e.type == "session/opened"
            ]
            assert len(opened) == 1
            assert opened[0].data.get("previous") == {"sid": "acp-gen-0"}
        finally:
            await asyncio.to_thread(settle)

    @pytest.mark.asyncio
    async def test_a_thread_route_is_still_bound_under_a_unified_scope(self) -> None:
        # A guild thread keys per-channel-peer regardless of dm_scope, so its
        # bucket still names one conversation.
        d, _cli, sess = _dispatcher({"u1"}, allowed_threads={"t9"}, dm_scope="unified")
        await d.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id="u1",
                conversation_id="t9",
                text="hello",
                thread_id="t9",
            )
        )
        assert sess.mirror_links[d._session_key("u1", "t9")].channel_id == "t9"

    @pytest.mark.asyncio
    async def test_a_dashboard_mirror_aimed_at_another_channel_survives(self) -> None:
        # The dashboard can aim this session's mirror at any surface. Overwriting
        # it on the next Discord message would silently redirect the owner's
        # replies from the chat they chose into this one.
        d, _cli, sess = _dispatcher({"u1"})
        key = d._session_key("u1")
        chosen = ChannelLink("telegram", channel_id="7", thread_id=None)
        sess.mirror_links[key] = chosen
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links == {key: chosen}

    @pytest.mark.asyncio
    async def test_a_resumed_session_is_not_bound_to_this_conversation(self) -> None:
        """A resumed dashboard session's own surface owns its output.

        Both writes live behind the ``resumed_key is None`` branch, which is what
        keeps a dashboard entry from being stamped with Discord's identity;
        ``set_origin_link`` is the observable half, since the mirror bind would
        decline anyway on finding the resume binding for this same channel.
        `!link` refuses in this state too, so the automatic path must not do what
        the explicit one declines.
        """
        d, cli, sess = _dispatcher({"u1"})
        resumed = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:chat-1"] = resumed
        sess.inbound_mirror_keys.add("dashboard:chat-1")
        await d.handle_message(self._msg("hello world"))
        assert "Answer: hello world" in (cli.final_text() or "")
        assert sess.mirror_links == {"dashboard:chat-1": resumed}
        assert sess.inbound_mirror_keys == {"dashboard:chat-1"}, "resume stayed two-way"
        assert sess.origin_links == {}

    @staticmethod
    def _occupy_ambiguously(sess: FakeSessions) -> None:
        """Occupy channel ``c1`` in the one state that reaches a refused claim.

        Discord declares ``supports_session_resume``, so its conversations are
        inbound-committable and an inbound-committed occupant refuses a claim. A
        single such occupant never gets that far — the dispatcher routes the turn
        to it and skips the bind, and `!link` refuses earlier. But
        ``resumed_session`` fails CLOSED on duplicates: with two inbound bindings
        it denies routing and reports none, so both paths proceed to a claim that
        is then refused.
        """
        for key in ("dashboard:chat-9", "dashboard:chat-10"):
            sess.mirror_links[key] = ChannelLink("discord", channel_id="c1")
            sess.inbound_mirror_keys.add(key)

    @pytest.mark.asyncio
    async def test_an_explicit_bind_to_another_channel_is_not_repointed(self) -> None:
        # Nothing repoints a binding: a swept or rival-claimed one is REMOVED, not
        # moved. So a discord binding naming another channel is deliberate (the
        # dashboard can bind a surfaced session anywhere).
        d, _cli, sess = _dispatcher({"u1"})
        key = d._session_key("u1")
        chosen = ChannelLink("discord", channel_id="c-elsewhere")
        sess.mirror_links[key] = chosen
        await d.handle_message(self._msg("hello"))
        assert sess.mirror_links == {key: chosen}

    @pytest.mark.asyncio
    async def test_unlink_clears_binding_stranded_by_generation_rotation(self) -> None:
        # THE stale-mirror regression: a binding written at one DM generation,
        # then the conversation rotates (!new / idle / daily reset). The row's
        # key spelling does not derive from the current session key, so the
        # key-addressed clears cannot reach it — yet it still occupies the
        # location and blocks `!session` resume. Unlink must free it by value.
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!link"))
        stale_key = d._session_key("u1")
        await d.handle_message(self._msg("!new"))  # rotate the generation
        key = d._session_key("u1")
        assert stale_key != key  # binding is now stranded under the old spelling
        assert stale_key not in (key, legacy_dashboard_mirror_key(key))
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_clears_dashboard_mirror_into_this_channel(self) -> None:
        # A dashboard session mirroring outbound into this conversation is the
        # exact occupant `!session`'s conflict check refuses on ("attached to
        # another session") — `!unlink` in the conversation must clear it.
        d, cli, sess = _dispatcher({"u1"})
        sess.mirror_links["dashboard:chat-9"] = ChannelLink("discord", channel_id="c1")
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_frees_a_paused_two_way_dashboard_mirror(self) -> None:
        # The shape a dashboard Disconnect leaves behind: the owner connected a
        # dashboard session to their own DM (two-way, because Discord resumes
        # inbound), then disconnected it from the dashboard. Disconnect only
        # PAUSES: the binding stays, the DM still routes here, and session
        # control keeps refusing the session. One `/unlink` in the DM must free
        # the location whatever the pause flag says, take the resumed-session
        # exit (the binding accepted inbound), and nudge the dashboard so the
        # chip and the menu stop showing a link that is gone.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:chat-42"] = loc
        sess.inbound_mirror_keys.add("dashboard:chat-42")
        sess.paused_deliveries.add(("dashboard:chat-42", False))
        pushes: list[None] = []
        d._session_resume._push_slots = lambda: pushes.append(None)  # type: ignore[method-assign]
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert sess.inbound_mirror_keys == set()
        assert any("Left the resumed session" in t for t, _ in cli.sent)
        assert pushes, "the dashboard projection was not refreshed after the sweep"
        # Idempotent: a second unlink finds the location free.
        await d.handle_message(self._msg("!unlink"))
        assert any("wasn't linked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_leaves_other_locations_alone(self) -> None:
        # The value sweep is exact-match: a mirror into a DIFFERENT Discord
        # channel must survive an unlink here, and with nothing pointing at
        # this conversation the reply stays truthful ("wasn't linked").
        d, cli, sess = _dispatcher({"u1"})
        other = ChannelLink("discord", channel_id="c2")
        sess.mirror_links["dashboard:chat-9"] = other
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {"dashboard:chat-9": other}
        assert any("wasn't linked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_frees_location_in_one_shot_with_resumed_session(self) -> None:
        # A resumed session AND an outbound dashboard mirror can co-occupy a
        # location: a session map can hold co-located bindings written before
        # conversations became exclusive. The resumed-session early path must
        # still free the WHOLE location — one `!unlink`, not two. The rows go in
        # directly because `set_mirror_link` refuses to create this state.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:resumed"] = loc
        sess.inbound_mirror_keys.add("dashboard:resumed")
        sess.mirror_links["dashboard:chat-9"] = loc
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Left the resumed session" in t for t, _ in cli.sent)
        await d.handle_message(self._msg("!unlink"))
        assert any("wasn't linked" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_unlink_repairs_duplicate_inbound_bindings(self) -> None:
        # Duplicate inbound bindings make the resolver fail closed. `!unlink`
        # repairs and settles them in the resume layer instead of falling through
        # to native unlink. Rows go in directly because the writer refuses them.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        for wedged in ("dashboard:wedged-a", "dashboard:wedged-b"):
            sess.mirror_links[wedged] = loc
            sess.inbound_mirror_keys.add(wedged)
        await d.handle_message(self._msg("!unlink"))
        assert sess.mirror_links == {}
        assert any("Left the resumed session" in t for t, _ in cli.sent)

    @pytest.mark.asyncio
    async def test_new_frees_whole_location_when_leaving_resumed_session(self) -> None:
        # `!new` releases a resumed session through the same whole-location
        # sweep as `!unlink`: a co-located outbound mirror must not leak into
        # the fresh conversation the command starts. The rows go in directly
        # because `set_mirror_link` refuses to create this state.
        d, cli, sess = _dispatcher({"u1"})
        loc = ChannelLink("discord", channel_id="c1")
        sess.mirror_links["dashboard:resumed"] = loc
        sess.inbound_mirror_keys.add("dashboard:resumed")
        sess.mirror_links["dashboard:bystander"] = loc
        await d.handle_message(self._msg("!new"))
        assert sess.mirror_links == {}
        assert any("left the resumed session" in t for t, _ in cli.sent)
