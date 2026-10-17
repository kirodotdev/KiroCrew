"""Telegram dashboard mirroring: /link, /unlink and the automatic origin mirror.

Covers how ``TelegramDispatcher`` (transport_dispatch.py) binds a Telegram conversation as
the mirror and origin of its session, so dashboard turns reach the chat: the explicit
/link and /unlink commands, the automatic bind on each inbound turn, forum topics, unified
DM scopes, and the crew-log opener a DM turn writes.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock

from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import FakeProvider, _dispatcher

from kiro_crew.messaging.link import ChannelLink, legacy_dashboard_mirror_key
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.telegram.commands import parse_command
from kiro_crew.telegram.transport import TelegramInboundMessage


class TestLinkCommand:
    def test_legacy_dashboard_mirror_key_transform(self) -> None:
        # Compat-only spelling: bindings written before session identity was
        # unified live on dashboard:<channel key with non-word chars folded>.
        assert (
            legacy_dashboard_mirror_key("telegram:kirocrew:direct:8743158320:gen3")
            == "dashboard:telegram_kirocrew_direct_8743158320_gen3"
        )
        assert (
            legacy_dashboard_mirror_key("telegram:kirocrew:direct:7")
            == "dashboard:telegram_kirocrew_direct_7"
        )

    def test_parse_link_unlink(self) -> None:
        assert parse_command("/link") == "link"
        assert parse_command("/unlink") == "unlink"
        assert parse_command("/LINK") == "link"
        assert parse_command("/new") == "new"
        assert parse_command("hello") is None

    def test_link_sets_mirror_on_channel_session_key(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        expected_key = d._session_key(("direct", "7"))
        assert expected_key in sess.mirror_links
        assert legacy_dashboard_mirror_key(expected_key) not in sess.mirror_links
        link = sess.mirror_links[expected_key]
        assert isinstance(link, ChannelLink)
        assert link.channel_type == "telegram"
        assert link.channel_id == "7"
        assert link.thread_id is None  # DM /link carries no Topic thread
        assert any("Linked" in t for t, _ in cli.sent)

    def test_forum_link_carries_topic_thread(self) -> None:
        # /link inside a forum Topic must store the Topic id
        # on the mirror link so dashboard-mirrored replies thread back into the
        # Topic (not the supergroup General).
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        route = ("forum", "-1001234567890:5")
        asyncio.run(d._handle_link(route, -1001234567890))
        link = sess.mirror_links[d._session_key(route)]
        assert link.channel_id == "-1001234567890"
        assert link.thread_id == "5"  # Topic id, as a str

    def test_unlink_clears_legacy_spelling(self) -> None:
        # A binding created before unification must still be clearable in-channel.
        d, cli, sess = _dispatcher({7})
        legacy = legacy_dashboard_mirror_key(d._session_key(("direct", "7")))
        sess.mirror_links[legacy] = ChannelLink("telegram", channel_id="7")
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_forum_unlink_sweeps_the_topic_scoped_location(self) -> None:
        # /link and /unlink must construct the SAME topic-scoped location, and
        # the sweep must not leak across Topics: a General-scoped (threadless)
        # binding in the same supergroup survives an unlink inside a Topic.
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        route = ("forum", "-1001234567890:5")
        asyncio.run(d._handle_link(route, -1001234567890))
        general = ChannelLink("telegram", channel_id="-1001234567890", thread_id=None)
        sess.mirror_links["dashboard:chat-9"] = general
        asyncio.run(d._handle_unlink(route, -1001234567890))
        assert sess.mirror_links == {"dashboard:chat-9": general}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_unlink_clears_existing(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_unlink_when_not_linked(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert any("wasn't linked" in t for t, _ in cli.sent)

    def test_unlink_clears_binding_stranded_under_foreign_spelling(self) -> None:
        # A binding whose key spelling does not derive from the current session
        # key (rotated DM generation, or a dashboard session mirroring into this
        # chat) still occupies the location. Unlink must clear it by location
        # value.
        d, cli, sess = _dispatcher({7})
        sess.mirror_links["dashboard:chat-9"] = ChannelLink("telegram", channel_id="7")
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_link_batches_its_writes_into_one_map_save(self) -> None:
        # /link makes three mutations. Each would otherwise rewrite the entire
        # session map, stalling the loop three times for one user action.
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        assert sess.batched_writes and all(sess.batched_writes)
        assert sess.batch_depth == 0

    def test_unlink_batches_its_writes_into_one_map_save(self) -> None:
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        sess.batched_writes.clear()
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.batched_writes and all(sess.batched_writes)
        assert sess.batch_depth == 0

    def test_unlink_leaves_other_locations_alone(self) -> None:
        # Exact-match sweep: a mirror into a DIFFERENT chat survives, and the
        # reply stays truthful when nothing points at this conversation.
        d, cli, sess = _dispatcher({7})
        other = ChannelLink("telegram", channel_id="8")
        sess.mirror_links["dashboard:chat-9"] = other
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {"dashboard:chat-9": other}
        assert any("wasn't linked" in t for t, _ in cli.sent)


class TestAutomaticOriginMirror:
    """A Telegram conversation mirrors itself, so dashboard turns reach the chat.

    Without the automatic mirror, a session started in Telegram has no
    ``telegram`` mirror unless the user types ``/link``, so
    ``_deliver_cross_surface_reply`` finds no target and a turn taken from the
    dashboard is never delivered back — the chat reads as dead while the
    conversation continues elsewhere.
    """

    @staticmethod
    def _turn(d: Any, uid: str = "7", text: str = "hi") -> None:
        asyncio.run(
            d.handle_message(
                InboundMessage(channel_type="telegram", user_id=uid, conversation_id=uid, text=text)
            )
        )

    def test_inbound_turn_binds_this_chat_as_the_mirror(self) -> None:
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        link = sess.mirror_links[d._session_key(("direct", "7"))]
        assert link == ChannelLink("telegram", channel_id="7", thread_id=None)

    def test_inbound_turn_records_this_chat_as_the_origin(self) -> None:
        # The same conversation the mirror is bound to, recorded as the session's
        # ORIGIN: the in-memory fact unattended output about the session and the
        # owner-DM check read. A mirror that equals it is the DM itself; one that
        # does not is a retarget, and only this record can tell the two apart.
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        key = d._session_key(("direct", "7"))
        assert sess.origin_links[key] == ChannelLink("telegram", channel_id="7", thread_id=None)
        assert sess.origin_links[key] == sess.mirror_links[key]

    def test_forum_turn_records_the_topic_as_the_origin(self) -> None:
        d, _cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="-1001234567890",
                    text="hi",
                    chat_type="supergroup",
                    thread_id="5",
                    message_id=1,
                )
            )
        )
        key = d._session_key(("forum", "-1001234567890:5"))
        assert sess.origin_links[key] == ChannelLink(
            "telegram", channel_id="-1001234567890", thread_id="5"
        )

    def test_a_unified_bucket_records_no_origin(self) -> None:
        # ``dm_scope="unified"`` collapses every allowed user's DMs into one
        # session, so "the origin conversation" has no single answer and recording
        # one user's chat would aim unattended output at whoever wrote last.
        d, _cli, sess = _dispatcher({7})
        d.cfg.messaging.dm_scope = "unified"
        self._turn(d)
        assert sess.origin_links == {}

    def test_a_dm_turn_opens_the_crew_log_the_work_ledger_writes_into(
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
        from unittest.mock import MagicMock

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.crew_log import emit, projection
        from kiro_crew.crew_log.resolve import unit_for_session_key
        from kiro_crew.dashboard.handlers import work_ledger as ledger_routes

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
        monkeypatch.setattr(FakeProvider, "session_id", "acp-owner-dm-turn", raising=False)
        ledger_routes._BOARD_LOCKS._locks.clear()

        async def _recognized(*a: Any, **k: Any) -> None:
            return None

        monkeypatch.setattr(ledger_routes, "_recognize_session", _recognized)
        monkeypatch.setattr(ledger_routes, "_is_restricted_session", lambda *a: False)
        monkeypatch.setattr(ledger_routes, "contained_channel_caller", lambda state, sk: "")

        async def _goal_write(sess: Any, key: str) -> tuple[int, dict[str, Any]]:
            app = web.Application()
            state = MagicMock()
            state.sessions = sess
            app["state"] = state
            req = make_mocked_request(
                "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": key}
            )
            req["internal_auth"] = True
            req.json = AsyncMock(  # type: ignore[method-assign]
                return_value={"action": "goal", "goal": "ship it", "round": 1}
            )
            resp = await ledger_routes.api_work_ledger_record(req)
            return resp.status, json.loads(resp.text)

        emit.reset_caches()
        try:
            d, _cli, sess = _dispatcher({7})
            self._turn(d)
            key = d._session_key(("direct", "7"))
            unit = unit_for_session_key(sess, key)
            assert unit == "acp-owner-dm-turn"
            status, body = asyncio.run(_goal_write(sess, key))
            assert (status, body.get("code")) == (200, None), body

            handle = projection.open_session_log(unit)
            assert handle is not None
            entries = list(handle.iter_from(1, known=projection.KNOWN_TYPES))
            opened = [e for e in entries if e.type == "session/opened"]
            assert len(opened) == 1
            assert opened[0].data["slot"] == key.replace(":", "_")
            assert "class" not in opened[0].data, "no live policy reader on this builder"
            assert [e.type for e in entries].count("work/recorded") == 1
        finally:
            emit.drain_for_shutdown(timeout=2.0)
            emit.reset_caches()
            ledger_routes._BOARD_LOCKS._locks.clear()

    def test_a_recycled_conversation_opens_its_successor_log_citing_the_predecessor(
        self, monkeypatch
    ) -> None:
        """The Telegram twin of the Discord succession pin: what the allocation
        boundary captured as the store this claim superseded -- after a compaction
        recycle, the stashed predecessor -- reaches ``on_session_opened`` as
        ``previous_sid``, consumed after ``get_or_create`` returns, so the successor's
        log cites the one it replaces. A store without the accessor hands over
        nothing, never a raise."""
        from kiro_crew.crew_log import emit as crew_log_emit

        opened: list[tuple[str, str]] = []
        monkeypatch.setattr(
            crew_log_emit,
            "on_session_opened",
            lambda session_id, **kw: opened.append((session_id, kw.get("previous_sid", ""))),
        )
        monkeypatch.setattr(FakeProvider, "session_id", "acp-gen-2", raising=False)
        d, _cli, sess = _dispatcher({7})
        # What the mapping named when the boundary registered this cold start.
        sess.mapped_sid = lambda key: "acp-gen-1"
        self._turn(d)
        assert opened == [("acp-gen-2", "acp-gen-1")]
        # A store that cannot answer hands over "" -- nothing to follow, never a raise.
        monkeypatch.setattr(sess, "allocation_predecessor", None)
        self._turn(d)
        assert opened[-1] == ("acp-gen-2", "")

    def test_the_opener_states_the_workspace_off_the_conversations_dashboard_slot(
        self, monkeypatch
    ) -> None:
        """The Telegram twin of the Discord one-class pin: ``workspace`` reaches
        ``on_session_opened`` from the slot the dashboard surfaces this conversation
        under -- the source a tab on it states the same fact from, so the two
        writers of one log never take turns recording a move. No slot yet (surfacing
        follows the first persisted turn) or no state attached states nothing, and a
        slot answering with something other than a string states nothing rather than
        its repr."""
        from types import SimpleNamespace

        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.dashboard.channel_slots import channel_slot_name

        stated: list[str] = []
        monkeypatch.setattr(
            crew_log_emit,
            "on_session_opened",
            lambda session_id, **kw: stated.append(kw.get("workspace", "<absent>")),
        )
        monkeypatch.setattr(FakeProvider, "session_id", "acp-ws", raising=False)
        d, _cli, _sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        slots: dict[str, object] = {}
        d.dashboard_state = SimpleNamespace(get_slot=slots.get)
        self._turn(d)
        assert stated == [""], "no slot yet: the opening entry states no workspace"
        slots[channel_slot_name(key)] = SimpleNamespace(workspace="ws-2")
        self._turn(d)
        assert stated[-1] == "ws-2"
        slots[channel_slot_name(key)] = SimpleNamespace(workspace=object())
        self._turn(d)
        assert stated[-1] == "", "a non-string answer is not a statement"
        d.dashboard_state = None
        self._turn(d)
        assert stated[-1] == ""

    def test_forum_turn_binds_the_topic_not_the_supergroup_general(self) -> None:
        # The bind shares _origin_mirror_link with /link, so a forum turn must
        # carry the Topic id — a General-scoped binding would thread dashboard
        # replies into the wrong place.
        d, _cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="-1001234567890",
                    text="hi",
                    chat_type="supergroup",
                    thread_id="5",
                    message_id=1,
                )
            )
        )
        route = ("forum", "-1001234567890:5")
        link = sess.mirror_links[d._session_key(route)]
        assert link == ChannelLink("telegram", channel_id="-1001234567890", thread_id="5")

    def test_unlink_survives_the_next_message(self) -> None:
        # The load-bearing half of the opt-out: mirroring is re-asserted every
        # turn, so without a PERSISTED refusal "off" would last one message.
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        self._turn(d, text="still off?")
        assert sess.mirror_links == {}

    def test_link_withdraws_the_opt_out_so_binding_resumes(self) -> None:
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        asyncio.run(d._handle_link(("direct", "7"), 7))
        sess.mirror_links.clear()  # prove the NEXT turn re-binds, not just /link
        self._turn(d)
        assert sess.mirror_links[d._session_key(("direct", "7"))] == ChannelLink(
            "telegram", channel_id="7", thread_id=None
        )

    def test_steady_state_turn_does_not_rewrite_an_identical_binding(self) -> None:
        # The bind is re-asserted every turn; skipping an unchanged write keeps
        # the per-turn cost a read instead of a session-map save.
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        writes: list[str] = []
        original = sess.set_mirror_link

        def _counting(key: str, link: Any) -> None:
            writes.append(key)
            original(key, link)

        sess.set_mirror_link = _counting  # type: ignore[method-assign]
        self._turn(d, text="second")
        assert writes == []

    def test_an_explicit_bind_to_a_different_chat_is_not_repointed(self) -> None:
        # Nothing repoints a binding: a swept or rival-claimed one is REMOVED,
        # not moved. So a telegram binding naming another chat is always
        # deliberate (the dashboard can bind a surfaced session anywhere), and
        # re-pointing it at the origin would undo an explicit action silently.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        chosen = ChannelLink("telegram", channel_id="999", thread_id=None)
        sess.mirror_links[key] = chosen
        self._turn(d)
        assert sess.mirror_links == {key: chosen}

    def test_the_first_turns_bucket_row_does_not_block_the_bind(
        self, tmp_path, monkeypatch
    ) -> None:
        # set_channel writes the legacy slack_channel_id bucket for a new telegram
        # session with no thread. The STORE reads that row as no mirror (a Slack
        # mirror is never synthesized without a thread), so the first turn's bind
        # lands; a Slack binding that names a thread is deliberate and is left alone.
        from kiro_crew.session_map import SessionMap

        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        store = SessionMap()
        d, _cli, sess = _dispatcher({7})
        sess.get_mirror_link = store.get_mirror_link
        sess.set_mirror_link = store.set_mirror_link
        key = d._session_key(("direct", "7"))
        store.set_slack_link(key, "", "telegram:7")
        assert store.get_mirror_link(key) is None
        self._turn(d)
        assert store.get_mirror_link(key) == ChannelLink("telegram", channel_id="7", thread_id=None)

        store.set_slack_link(key, "1786300000.000100", "C0OPS")
        store.clear_mirror_link(key)
        threaded = store.get_mirror_link(key)
        assert threaded == ChannelLink("slack", channel_id="C0OPS", thread_id="1786300000.000100")
        self._turn(d)
        assert store.get_mirror_link(key) == threaded, "a deliberate binding is never repointed"

    def test_the_refusal_survives_a_generation_rotation(self) -> None:
        # /new and the configured idle/daily reset rotate the :genN suffix. Keyed
        # per generation the flag would expire on rotation — an idle reset would
        # undo the user's /unlink with no action on their part, which is the very
        # failure the persisted flag exists to prevent.
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        before = d._session_key(("direct", "7"))
        d._conv.bump_gen(("direct", "7"))
        after = d._session_key(("direct", "7"))
        assert after != before, "generation did not rotate; test would be vacuous"
        assert sess.mirror_opt_out(after) is True

    def test_a_unified_dm_scope_is_not_auto_bound(self) -> None:
        # dm_scope=unified collapses every allowed user's DMs into one
        # unified:{agent} bucket — channel and user drop out of the key — so a
        # mirror bound there belongs to no particular chat and would deliver one
        # user's dashboard replies into another user's chat.
        d, _cli, sess = _dispatcher({7}, dm_scope="unified")
        self._turn(d)
        assert sess.mirror_links == {}

    def test_a_forum_route_is_still_bound_under_a_unified_scope(self) -> None:
        # A forum route keeps its full bucket under any dm_scope, so it names one
        # Topic and stays unambiguous.
        d, _cli, sess = _dispatcher(
            {7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890], dm_scope="unified"
        )
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="-1001234567890",
                    text="hi",
                    chat_type="supergroup",
                    thread_id="5",
                    message_id=1,
                )
            )
        )
        route = ("forum", "-1001234567890:5")
        assert sess.mirror_links[d._session_key(route)] == ChannelLink(
            "telegram", channel_id="-1001234567890", thread_id="5"
        )

    def test_a_refused_claim_does_not_break_the_turn(self) -> None:
        # set_mirror_link raises ConversationOwnershipConflict when a rival holds
        # the conversation. On the turn path an uncaught raise answers nothing.
        d, cli, sess = _dispatcher({7})

        def _refuse(key: str, link: Any) -> None:
            raise ConversationOwnershipConflict("held by another session")

        sess.set_mirror_link = _refuse  # type: ignore[method-assign]
        self._turn(d, text="hello world")
        assert cli.final_text() == "Answer: hello world"
        assert sess.successes == ["telegram:kirocrew:direct:7"]

    def test_the_binding_write_stays_on_the_loop_thread(self) -> None:
        # The write is BOUNDED — one whole-map rewrite, on a conversation's first
        # turn only — so the loop pays it inline rather than paying a thread hop.
        # Interleaving is not the reason: `session_map._MAP_LOCK` orders every
        # guarded mutation, `os.replace` included, so a worker could not drop a
        # persisted binding. Offloading would therefore be safe but pointless
        # here, and it would put an await between this bind and the turn it
        # belongs to. Pinned so the placement is a decision, not an accident.
        d, _cli, sess = _dispatcher({7})
        wrote_on: list[int] = []
        original = sess.set_mirror_link

        def _recording(key: str, link: Any) -> None:
            wrote_on.append(threading.get_ident())
            original(key, link)

        sess.set_mirror_link = _recording  # type: ignore[method-assign]
        self._turn(d)
        # asyncio.run drives the loop on THIS thread, so the loop's ident is ours.
        assert wrote_on == [threading.get_ident()]

    def test_an_explicit_relink_to_the_opted_out_chat_survives(self) -> None:
        # The user /unlinked, then deliberately rebound this session to this same
        # chat from the dashboard. That is indistinguishable by VALUE from an
        # unlink that half-landed, so a bind that "repaired" the state would
        # delete a link the user just made — re-creating the dead-chat symptom
        # this feature exists to fix, for a user who did everything right.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        explicit = ChannelLink("telegram", channel_id="7", thread_id=None)
        sess.set_mirror_opt_out(key, True)
        sess.mirror_links[key] = explicit
        self._turn(d)
        assert sess.mirror_links == {key: explicit}

    def test_the_opt_out_leaves_an_explicit_mirror_elsewhere_alone(self) -> None:
        # The dashboard can bind a surfaced session to any conversation. An
        # opt-out for THIS chat says nothing about that target, so repairing an
        # interrupted unlink must not double as deleting a link the user chose.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        elsewhere = ChannelLink("telegram", channel_id="999", thread_id="4")
        sess.mirror_links[key] = elsewhere
        sess.set_mirror_opt_out(key, True)
        self._turn(d)
        assert sess.mirror_links == {key: elsewhere}
