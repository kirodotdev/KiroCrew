"""Teams ``/sessions``: pick a dashboard chat and continue it in the Teams chat.

Every DECISION here is shared with Discord (``messaging/session_resume.py``) and its
routing/settlement state machine is covered by ``test_discord_sessions.py``. So this
suite pins the things that are Teams' own, and the ones a shared core cannot get right
on a channel's behalf:

* **owner-only, and stricter than Discord's rule.** Teams' allow-list routinely holds
  several people and a dashboard session is the OPERATOR's whole transcript, so listing
  is refused unless exactly one identity is configured.
* the picker is an Adaptive Card, and its payload carries an INDEX -- never a session
  key, which a client could forge into "bind whatever I named".
* routing runs BEFORE the command intercept, so `/compact` and `/stop` act on the
  session the user believes they are driving.
* `/new` and `/unlink` release the binding, and a release that cannot be made durable
  changes nothing and says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.history import transcript_stem
from kiro_crew.messaging import session_resume as core
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.teams.cards import KIND_SESSION
from kiro_crew.teams.client import TeamsInbound, TeamsSendError
from kiro_crew.teams.transport_dispatch import TeamsDispatcher

_SVC = "https://smba.trafficmanager.net/teams"
_OWNER = "owner@example.com"


#: The id Teams assigns the CARD the picker was posted as.
_CARD_ID = "card-1"


def _inbound(text: str, *, email: str = _OWNER, value: dict | None = None) -> TeamsInbound:
    """One inbound activity.

    A submit's own ``activity_id`` is deliberately DIFFERENT from ``reply_to_id``: a
    press is its own activity and ``replyToId`` is what points back at the card. A
    fixture that gives them the same value makes an "address the card" bug pass.
    """
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=email,
        activity_id="submit-9" if value else "act-1",
        reply_to_id=_CARD_ID if value else "",
        card_value=value,
    )


class _Client:
    def __init__(self, *, card_fails: bool = False) -> None:
        self.sent: list[str] = []
        self.cards: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.card_fails = card_fails

    async def send_message(self, conversation_id, content, service_url) -> str:
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def send_card(self, conversation_id, card, service_url) -> str:
        if self.card_fails:
            raise TeamsSendError("HTTP 502")
        self.cards.append(card)
        return _CARD_ID

    async def update_card(self, conversation_id, activity_id, card, service_url) -> bool:
        self.updated.append((activity_id, card))
        return True

    async def update_message(self, conversation_id, activity_id, content, service_url) -> bool:
        return True

    async def send_typing(self, conversation_id, service_url) -> None:
        return None


class _ConversationLog:
    """The slice the picker reads: a session list, metadata, and transcripts."""

    def __init__(self, rows: list[dict], logs: dict[str, list[dict]] | None = None) -> None:
        self.rows = rows
        self.logs = logs or {}
        self.metadata: dict[str, dict] = {}
        self.searched: list[str] = []

    def list_sessions(self) -> list[dict]:
        return list(self.rows)

    def search_sessions(self, query: str, limit: int) -> list[dict]:
        self.searched.append(query)
        needle = query.casefold()
        return [r for r in self.rows if needle in str(r.get("title", "")).casefold()]

    def get_metadata(self, key: str) -> dict:
        if key in self.metadata:
            return dict(self.metadata[key])
        for row in self.rows:
            if str(row.get("key", "")).endswith(key.removeprefix("dashboard:")):
                return {"title": row.get("title", "")}
        return {}

    def update_metadata_if(self, key: str, fields: dict, guard: Any) -> bool:
        if not guard(self.metadata.get(key, {})):
            return False
        self.metadata.setdefault(key, {}).update(fields)
        self.logs.setdefault(key, [])
        stem = transcript_stem(key)
        for row in self.rows:
            if row.get("key") == stem:
                row.update(fields)
                break
        else:
            self.rows.insert(0, {"key": stem, **fields})
        return True

    def has_log(self, key: str) -> bool:
        return key in self.logs

    def recent(self, key: str, count: int, roles: set[str]) -> list[dict]:
        return self.logs.get(key, [])[-count:]


class _Sessions:
    def __init__(self, *, flush_fails: bool = False) -> None:
        self.mirror_links: dict[str, ChannelLink] = {}
        self.inbound_keys: set[str] = set()
        self.flush_fails = flush_fails
        self.cleared: list[str] = []
        self.queues: dict[str, list] = {}
        self.channel_keys: set[str] = set()
        self.reserved_generations: set[str] = set()

    # -- mirror bindings --
    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        return [
            key
            for key, bound in self.mirror_links.items()
            if bound == link and (not inbound_only or key in self.inbound_keys)
        ]

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def has_mirror_row(self, key) -> bool:
        return key in self.mirror_links

    def set_mirror_link(
        self, key, link, *, accepts_inbound: bool = False, reason: str = ""
    ) -> None:
        self.mirror_links[key] = link
        if accepts_inbound:
            self.inbound_keys.add(key)

    def clear_mirror_links_at(self, link, *, reason: str = "") -> list:
        gone = [key for key, bound in self.mirror_links.items() if bound == link]
        for key in gone:
            self.mirror_links.pop(key, None)
            self.inbound_keys.discard(key)
        return gone

    async def aflush(self) -> None:
        if self.flush_fails:
            raise RuntimeError("disk full")

    # -- the rest of the dispatcher's slice --
    def is_busy(self, key) -> bool:
        return False

    def reserve_generation(self, session_key: str) -> None:
        self.reserved_generations.add(session_key)
        self.channel_keys.add(session_key)

    def max_generation(self, bucket: str) -> int:
        prefix = f"{bucket}:gen"
        return max(
            (
                int(key[len(prefix) :])
                for key in self.reserved_generations
                if key.startswith(prefix) and key[len(prefix) :].isdigit()
            ),
            default=-1,
        )

    def channel_key_for_stem(self, stem: str) -> str:
        for key in self.channel_keys | set(self.mirror_links):
            if transcript_stem(key) == stem:
                return key
        return ""

    def clear_queue(self, key, owned_by=None) -> None:
        self.cleared.append(key)

    def dequeue(self, key):
        return None

    def mirror_opt_out(self, key) -> bool:
        return False

    def set_mirror_opt_out(self, key, value) -> None:
        return None

    def clear_mirror_link(self, key, *, reason: str = "") -> bool:
        return self.mirror_links.pop(key, None) is not None

    def is_mirror_paused(self, key, *, origin: bool = False) -> bool:
        return False

    def batched_save(self):
        return contextlib.nullcontext()


def _dispatcher(
    sessions: Any, client: Any, log: Any, *, allowed: set[str] | None = None
) -> TeamsDispatcher:
    d = TeamsDispatcher(
        sessions=sessions,
        ctx_builder=SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_spawn=False)),
        cfg=SimpleNamespace(
            messaging=SimpleNamespace(
                queue_mode="steer", dm_scope="per_user", idle_reset_minutes=0, daily_reset_hour=-1
            ),
            agent=SimpleNamespace(default_agent="kirocrew", approval_mode="interactive"),
            teams=SimpleNamespace(soft_threshold_pct=80, hard_threshold_pct=95),
        ),
        conv_log=log,
        allowed_emails={_OWNER} if allowed is None else allowed,
    )
    d.client = client
    return d


def _rows(*titles: str) -> list[dict]:
    return [
        {"key": f"dashboard:chat-{i}", "title": title, "memory_mode": "persistent"}
        for i, title in enumerate(titles, 1)
    ]


def _press(card: dict, index: int) -> dict:
    return dict(card["content"]["actions"][index]["data"])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Each test gets its own expectation store; the file is process-global otherwise."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


class TestOwnerOnly:
    @pytest.mark.asyncio
    async def test_a_mixed_case_upn_is_still_the_owner(self) -> None:
        """Azure AD returns the UPN in directory case; the allow-list is lowercased.

        An exact compare would refuse the very identity the allow-list just admitted.
        """
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/sessions", email="Owner@Example.com"))

        assert client.cards, "the owner must be recognised whatever case Teams sends"

    @pytest.mark.asyncio
    async def test_a_shared_allow_list_cannot_list_sessions(self) -> None:
        """The operator's transcripts are not enumerable by everyone on the list.

        Two allow-listed identities means no owner, so `/sessions` refuses BOTH -- it
        does not silently pick the first entry.
        """
        client = _Client()
        d = _dispatcher(
            _Sessions(),
            client,
            _ConversationLog(_rows("Launch plan")),
            allowed={_OWNER, "other@example.com"},
        )

        await d.handle_message(_inbound("/sessions"))

        assert client.cards == [], "no list may be posted"
        assert "requires exactly one" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_non_owner_is_refused_and_audited(self, monkeypatch) -> None:
        events: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.messaging.session_resume.sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/sessions", email="stranger@example.com"))

        assert client.cards == []
        assert any(e["outcome"] == "denied" for e in events)

    @pytest.mark.asyncio
    async def test_a_forged_press_from_a_non_owner_binds_nothing(self) -> None:
        client = _Client()
        sessions = _Sessions()
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))
        await d.handle_message(_inbound("/sessions"))
        payload = _press(client.cards[0], 0)

        await d.handle_message(_inbound("", email="stranger@example.com", value=payload))

        assert sessions.mirror_links == {}


class TestThePicker:
    @pytest.mark.asyncio
    async def test_it_offers_the_recent_sessions_as_a_card(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan", "Billing")))

        await d.handle_message(_inbound("/sessions"))

        titles = [a["title"] for a in client.cards[0]["content"]["actions"]]
        assert titles == ["1. Launch plan", "2. Billing"]

    @pytest.mark.asyncio
    async def test_it_offers_only_this_identity_native_generations(self) -> None:
        prior_key = "teams:kirocrew:direct:owner@example.com:gen4"
        other_key = "teams:kirocrew:direct:other@example.com:gen4"
        rows = _rows("Launch plan") + [
            {
                "key": transcript_stem(prior_key),
                "title": "Earlier Teams generation",
                "memory_mode": "persistent",
            },
            {
                "key": transcript_stem(other_key),
                "title": "Another Teams identity",
                "memory_mode": "persistent",
            },
        ]
        log = _ConversationLog(rows, {"dashboard:chat-1": [], prior_key: [], other_key: []})
        client, sessions = _Client(), _Sessions()
        sessions.channel_keys.update({prior_key, other_key})
        d = _dispatcher(sessions, client, log)

        await d.handle_message(_inbound("/sessions"))

        titles = [a["title"] for a in client.cards[0]["content"]["actions"]]
        assert titles == ["1. Launch plan", "2. Earlier Teams generation"]

    @pytest.mark.asyncio
    async def test_the_payload_carries_an_index_never_a_session_key(self) -> None:
        """A submit is client input, so a key in it would be an instruction."""
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/sessions"))

        data = _press(client.cards[0], 0)
        assert set(data) == {"kc", "nonce", "index"}
        assert "dashboard" not in str(data)

    @pytest.mark.asyncio
    async def test_a_query_uses_the_dashboard_search(self) -> None:
        log = _ConversationLog(_rows("Launch plan", "Billing"))
        client = _Client()
        d = _dispatcher(_Sessions(), client, log)

        await d.handle_message(_inbound("/sessions billing"))

        assert log.searched == ["billing"], "the shared ranker, not a local title filter"
        titles = [a["title"] for a in client.cards[0]["content"]["actions"]]
        assert titles == ["1. Billing"]

    @pytest.mark.asyncio
    async def test_an_incognito_session_is_never_offered(self) -> None:
        rows = _rows("Launch plan")
        rows.append({"key": "dashboard:secret", "title": "Secret", "memory_mode": "incognito"})
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(rows))

        await d.handle_message(_inbound("/sessions"))

        titles = [a["title"] for a in client.cards[0]["content"]["actions"]]
        assert titles == ["1. Launch plan"], "resuming it would persist an unpersisted chat"

    @pytest.mark.asyncio
    async def test_no_matches_says_so_and_points_back(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/sessions nothing-like-this"))

        assert client.cards == []
        assert "No sessions matched" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_card_that_could_not_be_posted_registers_no_nonce(self) -> None:
        """Otherwise a press could resolve against a list nobody ever saw."""
        client = _Client(card_fails=True)
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/sessions"))

        assert len(d._session_resume.pickers) == 0
        assert "Couldn't show the session list" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_running_it_twice_retires_the_earlier_list(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/sessions"))
        stale = _press(client.cards[0], 0)
        await d.handle_message(_inbound("/sessions"))

        assert len(d._session_resume.pickers) == 1, "only the newest list stays live"
        assert stale["nonce"] != _press(client.cards[1], 0)["nonce"]


class TestPressing:
    @staticmethod
    def _log() -> _ConversationLog:
        return _ConversationLog(
            _rows("Launch plan"),
            {"dashboard:chat-1": [{"role": "assistant", "content": "prior work"}]},
        )

    @pytest.mark.asyncio
    async def test_a_press_binds_the_session_bidirectionally(self) -> None:
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, self._log())
        await d.handle_message(_inbound("/sessions"))

        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

        assert sessions.mirror_links == {"dashboard:chat-1": ChannelLink("teams", "CONV")}
        assert sessions.inbound_keys == {"dashboard:chat-1"}

    @pytest.mark.asyncio
    async def test_the_picker_is_replaced_by_its_outcome(self) -> None:
        """No row may still look pressable once one was chosen."""
        client = _Client()
        d = _dispatcher(_Sessions(), client, self._log())
        await d.handle_message(_inbound("/sessions"))

        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

        _activity_id, settled = client.updated[-1]
        assert settled["content"]["actions"] == []
        assert "Launch plan" in str(settled)

    @pytest.mark.asyncio
    async def test_the_transcript_tail_is_replayed(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, self._log())
        await d.handle_message(_inbound("/sessions"))

        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

        assert any("prior work" in body for body in client.sent), client.sent

    @pytest.mark.asyncio
    async def test_a_second_press_resolves_nothing(self) -> None:
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, self._log())
        await d.handle_message(_inbound("/sessions"))
        payload = _press(client.cards[0], 0)
        await d.handle_message(_inbound("", value=payload))
        sessions.mirror_links.clear()
        sessions.inbound_keys.clear()

        await d.handle_message(_inbound("", value=payload))

        assert sessions.mirror_links == {}, "a consumed choice cannot resume twice"

    @pytest.mark.asyncio
    async def test_an_expired_picker_says_so(self, monkeypatch) -> None:
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, self._log())
        await d.handle_message(_inbound("/sessions"))
        payload = _press(client.cards[0], 0)
        # Expire through the TTL constant, not by aging private state.
        monkeypatch.setattr(core, "PICKER_TTL_SECS", -1)

        await d.handle_message(_inbound("", value=payload))

        assert sessions.mirror_links == {}
        assert any("expired" in str(card) for _a, card in client.updated)

    @pytest.mark.asyncio
    async def test_an_out_of_range_index_binds_nothing(self) -> None:
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, self._log())
        await d.handle_message(_inbound("/sessions"))
        payload = _press(client.cards[0], 0)
        payload["index"] = 99

        await d.handle_message(_inbound("", value=payload))

        assert sessions.mirror_links == {}

    @pytest.mark.asyncio
    async def test_a_session_claimed_elsewhere_is_refused(self) -> None:
        client, sessions = _Client(), _Sessions()
        sessions.mirror_links["dashboard:chat-1"] = ChannelLink("discord", "chan-9")
        d = _dispatcher(sessions, client, self._log())
        await d.handle_message(_inbound("/sessions"))

        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

        assert sessions.mirror_links["dashboard:chat-1"] == ChannelLink("discord", "chan-9")
        assert any("already active on Discord" in str(c) for _a, c in client.updated)


class TestAddressingTheCardNotThePress:
    """A submit activity has its OWN id; ``replyToId`` points at the card.

    Passing the submit's id where the card's belongs makes every real press look like
    a press on a different posting, so the picker always answers "expired" -- and a
    fixture that reuses one id for both hides it completely.
    """

    @pytest.mark.asyncio
    async def test_a_press_resolves_against_the_card_it_came_from(self) -> None:
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(
            sessions,
            client,
            _ConversationLog(
                _rows("Launch plan"),
                {"dashboard:chat-1": [{"role": "assistant", "content": "prior"}]},
            ),
        )
        await d.handle_message(_inbound("/sessions"))
        press = _inbound("", value=_press(client.cards[0], 0))
        assert press.activity_id != press.reply_to_id, "the fixture must keep them apart"

        await d.handle_message(press)

        assert sessions.mirror_links, "the press must resolve against the card's id"

    @pytest.mark.asyncio
    async def test_a_press_naming_a_different_card_resolves_nothing(self) -> None:
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))
        await d.handle_message(_inbound("/sessions"))
        forged = _inbound("", value=_press(client.cards[0], 0))
        object.__setattr__(forged, "reply_to_id", "some-other-card")

        await d.handle_message(forged)

        assert sessions.mirror_links == {}


class TestRoutingComesFirst:
    @pytest.mark.asyncio
    async def test_a_resumed_turn_runs_under_the_dashboard_key(self, monkeypatch) -> None:
        seen: list[str] = []

        async def _drive(turn, **_kw):
            seen.append(turn.session_key)

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _drive)
        sessions = _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        d = _dispatcher(sessions, _Client(), _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("carry on"))

        assert seen == ["dashboard:chat-1"], "the turn must land in the resumed session"

    @pytest.mark.asyncio
    async def test_busy_native_generation_keeps_its_steer_after_picker_resume(
        self, monkeypatch
    ) -> None:
        prior = "teams:kirocrew:direct:owner@example.com:gen4"
        log = _ConversationLog(
            [
                {
                    "key": transcript_stem(prior),
                    "title": "Earlier Teams",
                    "memory_mode": "persistent",
                }
            ],
            {prior: []},
        )
        sessions, client = _Sessions(), _Client()
        sessions.channel_keys.add(prior)
        dispatcher = _dispatcher(sessions, client, log)
        monkeypatch.setattr(dispatcher, "_live_cfg", lambda: dispatcher.cfg)
        await dispatcher.handle_message(_inbound("/sessions"))
        await dispatcher.handle_message(_inbound("", value=_press(client.cards[0], 0)))
        assert (await dispatcher._session_resume.route("CONV")).resumed_key == prior
        provider = SimpleNamespace(
            supports_steer=True,
            has_active_turn=lambda: True,
            steer=AsyncMock(return_value=True),
        )
        sessions.is_busy = lambda key: key == prior
        sessions.get_provider = Mock(return_value=provider)
        client.sent.clear()

        await asyncio.wait_for(dispatcher.handle_message(_inbound("Check the remaining item")), 5)

        sessions.get_provider.assert_called_once_with(prior)
        provider.steer.assert_awaited_once_with("Check the remaining item")
        assert len(client.sent) == 1 and "Folded" in client.sent[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase", ["running", "finishing"])
    @pytest.mark.parametrize("attachment", [False, True], ids=["text", "attachment"])
    @pytest.mark.parametrize("peer_wake", [False, True], ids=["direct", "peer-wake"])
    async def test_teams_owned_resumed_dashboard_turn_drains_its_midturn_input(
        self, tmp_path, monkeypatch, phase, attachment, peer_wake
    ) -> None:
        from chat_test_helpers import _make_state, drain_background_tasks
        from test_teams_dispatch import FakeCtx, FakeProvider, FakeSessions

        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
        from kiro_crew.messaging.attachments import IngestResult
        from kiro_crew.messaging.queue_drain import wake_other_drains

        class ResumableSessions(FakeSessions, _Sessions):
            find_mirror_sessions = _Sessions.find_mirror_sessions
            set_mirror_link = _Sessions.set_mirror_link

            def __init__(self, provider):
                _Sessions.__init__(self)
                FakeSessions.__init__(self, provider, is_new=False)
                self._sessions = {}

            def _fold_key(self, key):
                return key

            async def get_or_create(self, key, **kwargs):
                result = await super().get_or_create(key, **kwargs)
                self._busy = True
                self._sessions[key] = SimpleNamespace(turn_owner=asyncio.current_task())
                self.acquired.append(key)
                return result

            def release(self, key):
                self._busy = False
                super().release(key)

        session_key = "dashboard:chat-1"
        provider = FakeProvider([])
        sessions, client = ResumableSessions(provider), _Client()
        dispatcher = _dispatcher(
            sessions, client, _ConversationLog(_rows("Navigation"), {session_key: []})
        )
        dispatcher.ctx_builder = FakeCtx()
        dispatcher.cfg.messaging.queue_mode = "queue"
        monkeypatch.setattr(dispatcher, "_live_cfg", lambda: dispatcher.cfg)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        dispatcher._session_resume.dashboard_state = state
        await dispatcher.handle_message(_inbound("/sessions"))
        await dispatcher.handle_message(_inbound("", value=_press(client.cards[0], 0)))
        assert (await dispatcher._session_resume.route("CONV")).resumed_key == session_key
        entered, release_stream = asyncio.Event(), asyncio.Event()
        release_persist = threading.Event()
        event_loop = asyncio.get_running_loop()
        messages, typing_tasks = [], []
        downloaded = tmp_path / "focus.txt"

        async def stream(message):
            messages.append(message)
            renderer = dispatcher._active_renderers[session_key]
            typing_tasks.append(renderer._typing_task)
            provider.active_turn = True
            try:
                if message == "begin" and phase == "running":
                    entered.set()
                    await asyncio.wait_for(release_stream.wait(), 10)
                if message != "begin" and attachment:
                    assert downloaded.exists()
                yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Navigation checked.")
                yield AcpEvent(kind=EVENT_COMPLETE)
            finally:
                provider.active_turn = False

        def persist(key, text, reply, is_new, agent):
            assert key == session_key
            if text == "begin" and phase == "finishing":
                event_loop.call_soon_threadsafe(entered.set)
                if not release_persist.wait(10):
                    raise TimeoutError("Teams persistence was not released")

        async def ingest(_client, descriptors):
            assert descriptors == inbound.attachments
            await asyncio.to_thread(downloaded.write_text, "focus report", encoding="utf-8")
            return IngestResult(file_paths=[str(downloaded)], text_blocks=["Focus report attached"])

        provider.stream = stream
        monkeypatch.setattr(dispatcher, "_persist_turn", persist)
        download = AsyncMock(side_effect=ingest)
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.process_teams_attachments", download
        )
        inbound = _inbound("Check Shift+Tab too")
        if attachment:
            inbound.attachments.append({"contentType": "text/plain", "name": "focus.txt"})
        if peer_wake:
            sessions._busy = True
            assert await dispatcher._enqueue_with_receipt(session_key, _inbound("begin"), "begin")
            sessions._busy = False
            turn = asyncio.create_task(
                wake_other_drains(waker="slack", session_key=session_key, channels=["teams"])
            )
        else:
            turn = asyncio.create_task(dispatcher.handle_message(_inbound("begin")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert sessions.is_busy(session_key) and not slot.running
            assert sessions._sessions[session_key].turn_owner is turn
            assert dispatcher._active_renderers[session_key]._finalized == (phase == "finishing")
            client.sent.clear()
            await asyncio.wait_for(dispatcher.handle_message(inbound), 5)
            queued = sessions.queues.get(session_key, [])
            assert len(queued) == 1, client.sent
            assert queued[0][1] == inbound.text
            assert queued[0][2]["attachments"] == inbound.attachments
            download.assert_not_awaited()
            assert not slot._queue and not slot._pending_steers
            release_stream.set()
            release_persist.set()
            await asyncio.wait_for(turn, 10)
            assert sessions.acquired == [session_key, session_key]
            assert sessions.released == [session_key, session_key]
            assert not dispatcher._executing_turn_tasks
            assert len(messages) == 2 and messages[1].startswith(inbound.text)
            assert not sessions.queues[session_key] and not sessions.is_busy(session_key)
            assert (await dispatcher._session_resume.route("CONV")).resumed_key == session_key
            if attachment:
                download.assert_awaited_once()
                assert "Focus report attached" in messages[1]
                assert not downloaded.exists()
            else:
                download.assert_not_awaited()
            assert not any("Send it again" in text for text in client.sent)
        finally:
            release_stream.set()
            release_persist.set()
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 10)
            await asyncio.wait_for(
                asyncio.gather(
                    *(task for task in typing_tasks if task is not None), return_exceptions=True
                ),
                5,
            )
            await asyncio.wait_for(drain_background_tasks(state), 5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case",
        [
            "foreign-owner",
            "missing-owner",
            "missing-state",
            "missing-slot",
            "closing",
            "remote",
            "wrong-session",
            "unknown-executor",
        ],
    )
    async def test_resumed_busy_refuses_unproven_or_unavailable_teams_owner(
        self, tmp_path, monkeypatch, case
    ) -> None:
        from chat_test_helpers import _make_state, drain_background_tasks

        session_key = "dashboard:chat-1"
        sessions, client = _Sessions(), _Client()
        sessions.set_mirror_link(session_key, ChannelLink("teams", "CONV"), accepts_inbound=True)
        lease = SimpleNamespace(turn_owner=None)
        sessions._sessions = {session_key: lease}
        sessions._fold_key = lambda key: key
        sessions.is_busy = lambda key: lease.turn_owner is not None
        sessions.get_provider = Mock(
            return_value=SimpleNamespace(
                supports_steer=True,
                has_active_turn=lambda: True,
                steer=AsyncMock(return_value=True),
            )
        )
        dispatcher = _dispatcher(sessions, client, _ConversationLog(_rows("Navigation")))
        monkeypatch.setattr(dispatcher, "_live_cfg", lambda: dispatcher.cfg)
        monkeypatch.setattr(dispatcher, "_enqueue_with_receipt", AsyncMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        dispatcher._session_resume.dashboard_state = state
        entered, release = asyncio.Event(), asyncio.Event()

        async def drive(turn, **kwargs):
            lease.turn_owner = asyncio.current_task()
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), 10)
            finally:
                await turn.renderer.close()

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", drive)
        turn = asyncio.create_task(dispatcher.handle_message(_inbound("begin")))
        foreign = asyncio.create_task(release.wait())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert lease.turn_owner is turn and turn in dispatcher._executing_turn_tasks
            assert not slot.running
            if case == "foreign-owner":
                lease.turn_owner = foreign
            elif case == "missing-owner":
                lease.turn_owner = None
            elif case == "missing-state":
                dispatcher._session_resume.dashboard_state = None
            elif case == "missing-slot":
                state._slots.pop(slot.key)
            elif case == "closing":
                slot.begin_close()
            elif case == "remote":
                slot.executor = "remote"
            elif case == "wrong-session":
                slot.linked_session_key = "teams:another"
            elif case == "unknown-executor":
                slot.executor = ""
            sessions.is_busy = lambda key: key == session_key
            client.sent.clear()

            await asyncio.wait_for(dispatcher.handle_message(_inbound("Check focus")), 5)

            assert len(client.sent) == 1 and "Send it again" in client.sent[0]
            sessions.get_provider.assert_not_called()
            dispatcher._enqueue_with_receipt.assert_not_awaited()
            assert not slot._queue and not slot._pending_steers
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(turn, foreign, return_exceptions=True), 10)
            await asyncio.wait_for(drain_background_tasks(state), 5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", ["success", "error", "cancel"])
    async def test_teams_turn_ownership_expires_before_drain_and_retained_choices(
        self, tmp_path, monkeypatch, ending
    ) -> None:
        from chat_test_helpers import _make_state, drain_background_tasks

        session_key = "dashboard:chat-1"
        sessions, client = _Sessions(), _Client()
        sessions.set_mirror_link(session_key, ChannelLink("teams", "CONV"), accepts_inbound=True)
        dispatcher = _dispatcher(sessions, client, _ConversationLog(_rows("Navigation")))
        monkeypatch.setattr(dispatcher, "_live_cfg", lambda: dispatcher.cfg)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        dispatcher._session_resume.dashboard_state = state
        entered, release = asyncio.Event(), asyncio.Event()
        lease = SimpleNamespace(turn_owner=None)
        sessions._sessions = {session_key: lease}
        sessions._fold_key = lambda key: key

        async def drive(turn, **kwargs):
            lease.turn_owner = asyncio.current_task()
            assert lease.turn_owner in dispatcher._executing_turn_tasks
            await turn.renderer.on_text_chunk("Pick one\n[OPTIONS: yes | no]")
            await turn.renderer.on_done()
            entered.set()
            try:
                await asyncio.wait_for(release.wait(), 10)
                if ending == "error":
                    raise RuntimeError("drive failed")
            finally:
                await turn.renderer.close()

        async def drain(key, inbound=None):
            assert key == session_key
            assert not dispatcher._executing_turn_tasks
            assert dispatcher._active_renderers[key].has_pending_choices

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", drive)
        drain_spy = AsyncMock(side_effect=drain)
        monkeypatch.setattr(dispatcher, "_drain_queue", drain_spy)
        turn = asyncio.create_task(dispatcher.handle_message(_inbound("begin")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert dispatcher._active_renderers[session_key].has_pending_choices
            assert turn in dispatcher._executing_turn_tasks
            if ending == "cancel":
                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(turn, 5)
            else:
                release.set()
                if ending == "error":
                    with pytest.raises(RuntimeError, match="drive failed"):
                        await asyncio.wait_for(turn, 5)
                else:
                    await asyncio.wait_for(turn, 5)
            assert not dispatcher._executing_turn_tasks
            assert dispatcher._active_renderers[session_key].has_pending_choices
            if ending == "success":
                drain_spy.assert_awaited_once()
            else:
                drain_spy.assert_not_awaited()

            # The retained renderer and old lease task cannot authorize a later input.
            sessions.is_busy = lambda key: key == session_key
            sessions.get_provider = Mock()
            enqueue = AsyncMock()
            monkeypatch.setattr(dispatcher, "_enqueue_with_receipt", enqueue)
            client.sent.clear()
            await asyncio.wait_for(dispatcher.handle_message(_inbound("Check focus")), 5)
            assert len(client.sent) == 1 and "Send it again" in client.sent[0]
            sessions.get_provider.assert_not_called()
            enqueue.assert_not_awaited()
            assert not slot._queue and not slot._pending_steers
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 10)
            await asyncio.wait_for(drain_background_tasks(state), 5)

    @pytest.mark.asyncio
    async def test_resumed_goal_consumes_human_criteria_correction(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import drain_background_tasks
        from test_dashboard_chat import TestRunChatSegmentFlush, _provider_mock

        from kiro_crew import autonudge, session_directive
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_STEER_CONSUMED,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            AcpEvent,
        )
        from kiro_crew.dashboard import chat_runner, session_control
        from kiro_crew.goal import GoalState, continuation_message
        from kiro_crew.goal_actions import goal_snapshot
        from kiro_crew.history import HUMAN_TURN_META_KEY

        state = TestRunChatSegmentFlush._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("chat-1")
        service = autonudge.AutoNudgeService(base_dir=tmp_path)
        monkeypatch.setattr(autonudge, "get_instance", lambda: service)
        goal = GoalState.from_dict(
            {"objective": "Verify keyboard navigation", "criteria": ["Arrow keys work"]}
        )
        provider = _provider_mock()
        provider.supports_steer = provider.client.supports_steer = True
        provider.steer_needs_loss_recovery = provider.client.steer_needs_loss_recovery = False
        provider.has_active_turn = Mock(return_value=True)
        provider.client.steer = AsyncMock(return_value=True)
        provider.steer = provider.client.steer
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions, client = _Sessions(), _Client()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        sessions.is_busy = lambda key: key == "dashboard:chat-1"
        sessions.get_provider = Mock(return_value=provider)
        dispatcher = _dispatcher(sessions, client, _ConversationLog(_rows("Navigation")))
        dispatcher._session_resume.dashboard_state = state
        monkeypatch.setattr(dispatcher, "_live_cfg", lambda: dispatcher.cfg)
        monkeypatch.setattr(dispatcher, "_enqueue_with_receipt", AsyncMock())
        monkeypatch.setattr(dispatcher, "_run_turn", AsyncMock())
        correction = "Also verify Shift+Tab returns focus"
        outcomes = []
        admissions = []
        real_apply = chat_runner.apply_session_directive

        async def capture(*args, **kwargs):
            result = await real_apply(*args, **kwargs)
            outcomes.append(
                (kwargs["producer_is_user_facing"], kwargs["producer_is_channel"], result)
            )
            return result

        monkeypatch.setattr(chat_runner, "apply_session_directive", capture)

        async def stream(message):
            await asyncio.wait_for(dispatcher.handle_message(_inbound(correction)), 5)
            admissions.append(slot._steer_admissions.get(correction))
            yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=correction)
            yield AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="criteria",
                title="goal",
                tool_name="goal",
                mcp_server_name=session_directive.CORE_MCP_SERVER,
            )
            yield AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="criteria",
                tool_final=True,
                tool_output=session_directive.encode(
                    "goal",
                    {
                        "action": "update",
                        "goal_id": before["goal_id"],
                        "generation": before["generation"],
                        "criteria": [*goal.criteria, correction],
                    },
                    "Goal change requested.",
                ),
            )
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="I have checked the navigation criteria.")
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        provider.stream = stream
        turn = None
        try:
            loop = await service.add(slot.key, continuation_message(goal), goal=goal, max_cycles=50)
            before = goal_snapshot(loop)
            budgets = (loop.max_cycles, loop.max_runtime_secs, loop.created_ts)
            turn = asyncio.create_task(
                chat_runner._run_chat(
                    state,
                    slot,
                    loop.message,
                    _directive_user_origin=False,
                    _directive_self_wake=True,
                    _directive_loop_id=loop.id,
                    _directive_loop_gen=loop.config_generation,
                )
            )
            slot.task = turn
            await asyncio.wait_for(turn, 10)
            provider.client.steer.assert_awaited_once_with(correction)
            assert any("Folded" in text for text in client.sent), client.sent
            assert len(outcomes) == 1
            assert outcomes[0][:2] == (True, True), outcomes
            assert outcomes[0][2].startswith("Goal updated:"), outcomes
            assert loop.goal.criteria == [*goal.criteria, correction]
            assert loop.id == before["goal_id"] and loop.goal.objective == goal.objective
            assert (loop.max_cycles, loop.max_runtime_secs, loop.created_ts) == budgets
            assert not slot._queue and not slot._pending_steers
            rows = [row for row in slot.messages if row.get("role") == "user"]
            row = next(row for row in rows if row.get("content") == correction)
            assert row["meta"]["steerState"] == "consumed"
            assert row["meta"][HUMAN_TURN_META_KEY] is True
            (admission,) = admissions
            assert admission[session_control.CHANNEL_RECIPIENT_META_KEY] == {
                "channel_type": "teams",
                "conversation_id": "CONV",
                "principal": _OWNER,
            }
            assert not slot._steer_admissions
            assert not slot._steer_user_origin and not slot._steer_channel_origin
            dispatcher._enqueue_with_receipt.assert_not_awaited()
            dispatcher._run_turn.assert_not_awaited()
        finally:
            if turn is not None and not turn.done():
                turn.cancel()
                await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 5)
            tasks = list(service._timers.values()) + list(service._inflight_adds)
            service.stop()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            await asyncio.wait_for(drain_background_tasks(state), 5)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "case,expected",
        [
            ("config-queue", "queued"),
            ("queue-override", "queued"),
            ("steer-override", "steered"),
            ("declined-steer", "queued"),
            ("no-client", "queued"),
            ("attachments", "refused"),
            ("missing", "refused"),
            ("closing", "refused"),
            ("idle", "refused"),
            ("remote", "refused"),
        ],
    )
    async def test_resumed_busy_input_keeps_dashboard_delivery_ownership(
        self, tmp_path, monkeypatch, case, expected
    ) -> None:
        from chat_test_helpers import _make_state, drain_background_tasks

        from kiro_crew.dashboard import session_control
        from kiro_crew.history import HUMAN_TURN_META_KEY

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        released = asyncio.Event()
        steer_client = SimpleNamespace(
            supports_steer=True,
            steer_needs_loss_recovery=False,
            steer=AsyncMock(return_value=case != "declined-steer"),
        )
        slot._acp_client = None if case == "no-client" else steer_client
        if case == "missing":
            state._slots.pop(slot.key)
        elif case == "closing":
            slot.begin_close()
        elif case == "remote":
            slot.executor = "remote"
        sessions, client = _Sessions(), _Client()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        sessions.is_busy = lambda key: key == "dashboard:chat-1"
        sessions.get_provider = Mock(return_value=steer_client)
        dispatcher = _dispatcher(sessions, client, _ConversationLog(_rows("Navigation")))
        dispatcher._session_resume.dashboard_state = state
        if case in {"config-queue", "steer-override"}:
            dispatcher.cfg.messaging.queue_mode = "queue"
        monkeypatch.setattr(dispatcher, "_live_cfg", lambda: dispatcher.cfg)
        monkeypatch.setattr(dispatcher, "_enqueue_with_receipt", AsyncMock())
        monkeypatch.setattr(dispatcher, "_run_turn", AsyncMock())
        download = AsyncMock()
        monkeypatch.setattr(
            "kiro_crew.teams.transport_dispatch.process_teams_attachments", download
        )
        text = "Check Shift+Tab too"
        prefix = {"queue-override": "/queue ", "steer-override": "/steer "}.get(case, "")
        inbound = _inbound(prefix + text)
        if case == "attachments":
            inbound.attachments.append({"contentType": "image/png", "name": "focus.png"})
        busy = asyncio.create_task(released.wait())
        try:
            slot.task = busy if case != "idle" else None
            await asyncio.wait_for(dispatcher.handle_message(inbound), 5)
            assert len(client.sent) == 1
            if expected == "steered":
                steer_client.steer.assert_awaited_once_with(text)
                assert "Folded" in client.sent[0]
                assert not slot._queue
                assert slot._pending_steers == [text]
                assert slot._steer_user_origin == {text: True}
                assert slot._steer_channel_origin == {text: True}
                row = next(row for row in slot.messages if row.get("content") == text)
                assert row["meta"][HUMAN_TURN_META_KEY] is True
                admission = slot._steer_admissions[text]
            elif expected == "queued":
                assert "Queued" in client.sent[0]
                (entry,) = slot._queue
                assert entry["content"] == text
                assert entry["_directive_user_origin"] is True
                assert entry["_directive_channel_origin"] is True
                assert not slot._pending_steers
                admission = entry["meta"]
                if case == "declined-steer":
                    steer_client.steer.assert_awaited_once_with(text)
                else:
                    steer_client.steer.assert_not_awaited()
            else:
                assert "Send it again" in client.sent[0]
                assert "Folded" not in client.sent[0] and "Queued" not in client.sent[0]
                if case == "attachments":
                    assert "attachments" in client.sent[0]
                    assert inbound.attachments == [
                        {"contentType": "image/png", "name": "focus.png"}
                    ]
                steer_client.steer.assert_not_awaited()
                assert not slot._queue and not slot._pending_steers
            if expected != "refused":
                assert session_control.QUEUED_CONTAINMENT_META_KEY in admission
                assert admission[session_control.CHANNEL_RECIPIENT_META_KEY] == {
                    "channel_type": "teams",
                    "conversation_id": "CONV",
                    "principal": _OWNER,
                }
            sessions.get_provider.assert_not_called()
            dispatcher._enqueue_with_receipt.assert_not_awaited()
            dispatcher._run_turn.assert_not_awaited()
            download.assert_not_awaited()
        finally:
            released.set()
            await asyncio.wait_for(busy, 5)
            slot.task = None
            await asyncio.wait_for(drain_background_tasks(state), 5)

    @pytest.mark.asyncio
    async def test_stop_targets_the_resumed_session_not_the_native_one(self) -> None:
        """Cancelling the native session leaves the one the user is watching running."""
        stopped: list[str] = []

        client, sessions = _Client(), _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        async def _stop(sessions_arg, key, **_kw):
            stopped.append(key)
            return "Stopped."

        import kiro_crew.teams.transport_dispatch as mod

        real = mod.stop_running_turn
        try:
            mod.stop_running_turn = _stop  # type: ignore[assignment]
            await d.handle_message(_inbound("/stop"))
        finally:
            mod.stop_running_turn = real  # type: ignore[assignment]

        assert stopped == ["dashboard:chat-1"]

    @pytest.mark.asyncio
    async def test_compact_targets_the_resumed_session_too(self) -> None:
        client, sessions = _Client(), _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        acquired: list[str] = []
        sessions.try_acquire = lambda key: _record_and_refuse(acquired, key)  # type: ignore
        sessions.has_session = lambda key: False  # type: ignore
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/compact"))

        assert acquired == ["dashboard:chat-1"]

    @pytest.mark.asyncio
    async def test_a_detached_binding_refuses_the_message(self) -> None:
        """The record survives the binding, which is what makes the loss reportable."""
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))
        await d._session_resume._expectations.record("CONV", "dashboard:chat-1", "Launch plan")

        await d.handle_message(_inbound("still there?"))

        assert "Detached" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_a_refusal_that_never_landed_settles_nothing(self) -> None:
        """An undelivered refusal must not advance the routing state.

        Settling clears the record the refusal was owed for, so the NEXT message routes
        into the conversation's own session with the user never having been told their
        link was gone. An unsettled record owes the same refusal again, which is the
        direction that fails safe.
        """

        class _Deaf(_Client):
            async def send_message(self, conversation_id, content, service_url) -> str:
                raise TeamsSendError("HTTP 502")

        client, sessions = _Deaf(), _Sessions()
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))
        expectations = d._session_resume._expectations
        await expectations.record("CONV", "dashboard:chat-1", "Launch plan")

        await d.handle_message(_inbound("still there?"))

        record = await expectations.get("CONV")
        assert record is not None and not record.retired, "the record still owes a refusal"

    @pytest.mark.asyncio
    async def test_sessions_stays_reachable_while_routing_refuses(self) -> None:
        """A user whose link broke needs the way back IN, not just the refusal."""
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))
        await d._session_resume._expectations.record("CONV", "dashboard:chat-1", "Launch plan")

        await d.handle_message(_inbound("/sessions"))

        assert client.cards, "the picker must still be reachable"

    @pytest.mark.asyncio
    async def test_an_ambiguous_conversation_is_refused_not_guessed(self) -> None:
        client, sessions = _Client(), _Sessions()
        for key in ("dashboard:chat-1", "dashboard:chat-2"):
            sessions.set_mirror_link(key, ChannelLink("teams", "CONV"), accepts_inbound=True)
        d = _dispatcher(sessions, client, _ConversationLog(_rows("A", "B")))

        await d.handle_message(_inbound("hello"))

        assert "Ambiguous link" in client.sent[-1]


async def _record_and_refuse(seen: list, key: str) -> bool:
    """Record which session a command tried to acquire, then refuse it."""
    seen.append(key)
    return False


class TestAClickInAResumedConversation:
    """An Approve press and an option chip must reach the session the turn ran in.

    The turn registers its decider and its renderer under the RESUMED key, so a click
    resolved against the native ``teams:{email}`` session finds neither -- and the user is
    told the prompt is stale while the tool goes on to deny by default at the timeout.
    """

    @staticmethod
    def _resumed(client: Any, log: Any) -> tuple[TeamsDispatcher, Any]:
        sessions = _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        return _dispatcher(sessions, client, log), sessions

    @pytest.mark.asyncio
    async def test_approving_resolves_the_resumed_sessions_prompt(self) -> None:
        from kiro_crew.teams.approvals import TeamsApprovalDecider
        from kiro_crew.teams.cards import DECISION_APPROVE, KIND_APPROVAL

        client = _Client()
        d, _ = self._resumed(client, _ConversationLog(_rows("Launch plan")))

        decider = TeamsApprovalDecider(session_key="dashboard:chat-1")
        decider.arm("1", "n1")
        pending = asyncio.ensure_future(decider(SimpleNamespace(request_id="1")))
        await asyncio.sleep(0)
        try:
            await d._handle_card_action(
                _inbound(
                    "",
                    value={
                        "kc": KIND_APPROVAL,
                        "rid": "1",
                        "nonce": "n1",
                        "decision": DECISION_APPROVE,
                    },
                )
            )

            assert await pending is True, "the press must resolve the resumed turn's prompt"
        finally:
            pending.cancel()
        assert not any("no longer waiting" in text for text in client.sent)

    @pytest.mark.asyncio
    async def test_a_chip_runs_the_label_the_resumed_turn_offered(self, monkeypatch) -> None:
        from kiro_crew.teams.cards import KIND_OPTION
        from kiro_crew.teams.renderer import TeamsRenderer
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        seen: list[tuple[str, str]] = []

        async def _drive(turn, **_kw):
            seen.append((turn.session_key, turn.user_text))

        monkeypatch.setattr("kiro_crew.teams.transport_dispatch.drive_turn", _drive)
        client = _Client()
        d, _ = self._resumed(client, _ConversationLog(_rows("Launch plan")))

        renderer = TeamsRenderer(
            client, "CONV", _SVC, TEAMS_CAPABILITIES, session_key="dashboard:chat-1"
        )
        renderer._option_nonce = "n9"
        renderer._option_labels = ["ship it", "wait"]
        d._active_renderers["dashboard:chat-1"] = renderer

        await d._handle_card_action(
            _inbound(
                "",
                value={"kc": KIND_OPTION, "nonce": "n9", "index": "0", "label": "ship it"},
            )
        )

        assert seen == [("dashboard:chat-1", "ship it")]

    @pytest.mark.asyncio
    async def test_a_turn_that_started_before_the_bind_still_resolves(self) -> None:
        """A card click is a relief activity, so a pick can bind mid-turn.

        The in-flight turn keeps running under the key it started with, so BOTH keys
        have to be tried -- resolving only the resumed one would strand it.
        """
        from kiro_crew.teams.approvals import TeamsApprovalDecider
        from kiro_crew.teams.cards import DECISION_DENY, KIND_APPROVAL

        client = _Client()
        d, _ = self._resumed(client, _ConversationLog(_rows("Launch plan")))

        native = TeamsApprovalDecider(session_key=d._session_key(_OWNER))
        native.arm("1", "n1")
        pending = asyncio.ensure_future(native(SimpleNamespace(request_id="1")))
        await asyncio.sleep(0)
        try:
            await d._handle_card_action(
                _inbound(
                    "",
                    value={
                        "kc": KIND_APPROVAL,
                        "rid": "1",
                        "nonce": "n1",
                        "decision": DECISION_DENY,
                    },
                )
            )

            assert await pending is False
        finally:
            pending.cancel()


class TestLeaving:
    @pytest.mark.asyncio
    async def test_unlink_releases_the_resumed_binding(self) -> None:
        client, sessions = _Client(), _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/unlink"))

        assert sessions.mirror_links == {}
        assert "resumed dashboard session" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_new_releases_it_too(self) -> None:
        client, sessions = _Client(), _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/new"))

        assert sessions.mirror_links == {}
        assert "left the resumed dashboard session" in client.sent[-1]

    @pytest.mark.asyncio
    async def test_repeated_new_does_not_materialize_empty_history_rows(self) -> None:
        client, sessions = _Client(), _Sessions()
        log = _ConversationLog(_rows("Launch plan"), {"dashboard:chat-1": []})
        d = _dispatcher(sessions, client, log)

        await d.handle_message(_inbound("/new"))
        first_key = d._session_key(_OWNER)
        await d.handle_message(_inbound("/new"))
        second_key = d._session_key(_OWNER)

        assert first_key != second_key
        assert not log.has_log(first_key)
        assert not log.has_log(second_key)
        assert {first_key, second_key} <= sessions.reserved_generations

    @pytest.mark.asyncio
    async def test_a_release_that_is_not_durable_changes_nothing_and_says_so(self) -> None:
        """A cleared owner whose flush failed would run natively in silence until the
        persisted binding revived on restart, splitting one history in two."""
        client = _Client()
        sessions = _Sessions(flush_fails=True)
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/unlink"))

        assert "NOT completed" in client.sent[-1]
        # "Changes nothing" has to be true of the LIVE map too, not just the file: the
        # in-memory clear already happened, so without a rollback the user is told the
        # release failed while their next message routes to their own session.
        assert sessions.mirror_links == {"dashboard:chat-1": ChannelLink("teams", "CONV")}
        assert sessions.inbound_keys == {"dashboard:chat-1"}

    @pytest.mark.asyncio
    async def test_a_rollback_does_not_widen_an_observe_only_mirror(self) -> None:
        """Restoring must put back the shape that was there, not a more permissive one.

        A co-located occupant that did NOT accept inbound is a session this conversation
        was only observing; handing it inbound on the way back would let the conversation
        drive it.
        """
        client = _Client()
        sessions = _Sessions(flush_fails=True)
        link = ChannelLink("teams", "CONV")
        sessions.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        sessions.set_mirror_link("dashboard:observed", link, accepts_inbound=False)
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/unlink"))

        assert sessions.mirror_links == {"dashboard:chat-1": link, "dashboard:observed": link}
        assert sessions.inbound_keys == {"dashboard:chat-1"}

    @pytest.mark.asyncio
    async def test_unlink_with_nothing_resumed_still_works(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")))

        await d.handle_message(_inbound("/unlink"))

        assert client.sent, "the ordinary mirror opt-out still answers"
        assert "resumed dashboard session" not in client.sent[-1]


class TestSharedWithDiscord:
    def test_the_expectation_stores_are_separate_files(self) -> None:
        """A Discord channel id and a Teams conversation id are unrelated spaces.

        One shared file would let one channel's row answer for the other's
        conversation -- a mis-route of somebody's transcript.
        """
        from kiro_crew.messaging.resume_expectation import store_filename

        assert store_filename("teams") != store_filename("discord")

    def test_teams_and_discord_use_one_picker_bind_controller(self) -> None:
        from kiro_crew.discord.session_resume import DiscordSessionResume
        from kiro_crew.teams.session_resume import TeamsSessionResume

        sessions = _Sessions()
        log = _ConversationLog(_rows("Launch plan"))
        discord = DiscordSessionResume(sessions, log, {"u1"})
        teams = TeamsSessionResume(sessions, log, {_OWNER})

        assert type(discord._controller) is core.SessionResumeController
        assert type(teams._controller) is core.SessionResumeController
        assert type(discord._binder) is core.SessionBinder
        assert type(teams._binder) is core.SessionBinder

    @pytest.mark.asyncio
    async def test_controller_uses_the_surface_expectation_identity(self) -> None:
        """A channel may have several conversations under one channel id.

        Telegram forum Topics are the motivating shape: recording only chat_id
        would let every Topic in one supergroup overwrite the same expectation.
        """
        from kiro_crew.teams.session_resume import TeamsSessionResume, _TeamsResumeSurface

        client = _Client()
        sessions = _Sessions()
        resume = TeamsSessionResume(sessions, TestPressing._log(), {_OWNER})
        surface = _TeamsResumeSurface(client, "CONV", _SVC)
        surface.expectation_id = "CONV:TOPIC"
        choice = core.SessionChoice(key="dashboard:chat-1", title="Launch plan")
        nonce = resume.pickers.mint()
        resume.pickers.register(nonce, _OWNER, _CARD_ID, (choice,))

        selected = await resume._controller.choose(
            surface,
            caller=_OWNER,
            picker_owner=_OWNER,
            is_owner=True,
            message_id=_CARD_ID,
            nonce=nonce,
            index=0,
            link=ChannelLink("teams", "CONV", "TOPIC"),
        )

        assert selected == choice
        record = await resume._expectations.get("CONV:TOPIC")
        assert record is not None and record.key == choice.key
        assert await resume._expectations.get("CONV") is None

    def test_the_teams_card_kind_is_distinct_from_the_other_two(self) -> None:
        """So an approval press and a session press can never be confused."""
        from kiro_crew.teams.cards import KIND_APPROVAL, KIND_OPTION

        assert len({KIND_SESSION, KIND_APPROVAL, KIND_OPTION}) == 3


def test_the_picker_registry_scopes_by_owner_and_message() -> None:
    """A press must match the nonce, the owner AND the posting it came from."""
    registry = core.PickerRegistry()
    choices = (core.SessionChoice(key="dashboard:a", title="A"),)
    nonce = registry.mint()
    registry.register(nonce, "owner", "msg-1", choices)

    assert registry.take(nonce, 0, "someone-else", "msg-1") is None
    assert registry.take(nonce, 0, "owner", "msg-2") is None
    assert registry.take("other-nonce", 0, "owner", "msg-1") is None
    assert registry.take(nonce, 5, "owner", "msg-1") is None
    assert registry.take(nonce, 0, "owner", "msg-1") == choices[0]
    assert registry.take(nonce, 0, "owner", "msg-1") is None, "consumed on success"


def test_a_stacked_transcript_prefix_normalizes_to_the_canonical_key() -> None:
    """Stripping one layer would bind a key no session has, resuming nothing."""
    assert core.history_dashboard_key("dashboard_dashboard_chat-1") == "dashboard:chat-1"
    assert core.history_dashboard_key("dashboard_chat-1") == "dashboard:chat-1"
    assert core.history_dashboard_key("dashboard:chat-1") == "dashboard:chat-1"
    assert core.history_dashboard_key("slack:1755000000.1") is None
    assert core.history_dashboard_key("dashboard_") is None


@pytest.mark.asyncio
async def test_an_empty_allow_list_lists_nothing() -> None:
    """Deny-by-default: no configured identity means no owner, so no listing."""
    client = _Client()
    d = _dispatcher(_Sessions(), client, _ConversationLog(_rows("Launch plan")), allowed=set())

    await d.handle_message(_inbound("/sessions"))

    assert client.cards == []


@pytest.mark.asyncio
async def test_sessions_is_in_the_command_table_and_help() -> None:
    """One table drives the parser AND /help, so the two cannot drift."""
    from kiro_crew.teams.commands import COMMAND_SPEC, build_help_text, parse_command

    assert parse_command("/sessions") == "sessions"
    assert any(canonical == "sessions" for canonical, _a, _d in COMMAND_SPEC)
    assert "/sessions" in build_help_text()
    await asyncio.sleep(0)


class TestWhenListingCannotHappen:
    """Every dead end says WHY, because a silent `/sessions` is a broken bot."""

    @pytest.mark.asyncio
    async def test_no_history_store_says_so(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, None)

        await d.handle_message(_inbound("/sessions"))

        assert "unavailable" in client.sent[-1]
        assert not client.cards

    @pytest.mark.asyncio
    async def test_a_listing_failure_says_so_and_is_audited(self, monkeypatch) -> None:
        """A read that raised is not "no sessions" — telling them apart is the point."""
        rows: list[tuple[str, str]] = []

        class _Log(_ConversationLog):
            def list_sessions(self) -> list[dict]:
                raise OSError("disk gone")

        client = _Client()
        d = _dispatcher(_Sessions(), client, _Log(_rows("Launch plan")))
        monkeypatch.setattr(
            "kiro_crew.messaging.session_resume.sel",
            lambda: SimpleNamespace(
                log_api_access=lambda **kw: rows.append((kw["operation"], kw["outcome"]))
            ),
        )

        await d.handle_message(_inbound("/sessions"))

        assert "unavailable" in client.sent[-1]
        assert ("teams.sessions_data_access", "error") in rows

    @pytest.mark.asyncio
    async def test_an_empty_history_says_there_are_none(self) -> None:
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog([]))

        await d.handle_message(_inbound("/sessions"))

        assert client.sent[-1] == "No recent sessions."

    @pytest.mark.asyncio
    async def test_the_heading_says_how_much_was_cut(self) -> None:
        """A list capped at 10 of 14 must not read as "these are all your sessions"."""
        client = _Client()
        d = _dispatcher(_Sessions(), client, _ConversationLog(_rows(*[f"S{i}" for i in range(14)])))

        await d.handle_message(_inbound("/sessions"))

        heading = client.cards[0]["content"]["body"][0]["text"]
        assert "10 of 14" in heading

    @pytest.mark.asyncio
    async def test_a_search_heading_names_the_query_and_the_cut(self) -> None:
        client = _Client()
        rows = _rows(*[f"plan {i}" for i in range(14)])
        d = _dispatcher(_Sessions(), client, _ConversationLog(rows))

        await d.handle_message(_inbound("/sessions plan"))

        heading = client.cards[0]["content"]["body"][0]["text"]
        assert "10 of 14" in heading and "plan" in heading


class TestWhenAPressCannotTakeEffect:
    """A press that could not bind must say so and leave nothing half-bound."""

    @staticmethod
    async def _pressed(d: TeamsDispatcher, client: _Client) -> None:
        await d.handle_message(_inbound("/sessions"))
        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

    @pytest.mark.asyncio
    async def test_a_session_whose_log_vanished(self) -> None:
        client, sessions = _Client(), _Sessions()
        # Listed (so it is offerable) but with no transcript behind it.
        d = _dispatcher(sessions, client, _ConversationLog(_rows("Launch plan")))

        await self._pressed(d, client)

        assert "no longer available" in str(client.updated[-1][1])
        assert sessions.mirror_links == {}

    @pytest.mark.asyncio
    async def test_a_binding_that_could_not_be_recorded_binds_nothing(self) -> None:
        """The record is written BEFORE the banner, so a failed write must refuse."""
        from kiro_crew.messaging.resume_expectation import ExpectationStoreError

        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, TestPressing._log())

        async def _boom(*_a, **_kw):
            raise ExpectationStoreError("disk full")

        d._session_resume._binder.expectations.record = _boom  # type: ignore[assignment]

        await self._pressed(d, client)

        assert "was NOT resumed" in str(client.updated[-1][1])
        assert sessions.mirror_links == {}

    @pytest.mark.asyncio
    async def test_an_untyped_expectation_failure_still_binds_nothing(self) -> None:
        """Store path resolution can fail before errors become domain exceptions."""
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, TestPressing._log())

        async def _boom(*_a, **_kw):
            raise RuntimeError("path resolution failed")

        d._session_resume._binder.expectations.record = _boom  # type: ignore[assignment]

        await self._pressed(d, client)

        assert "was NOT resumed" in str(client.updated[-1][1])
        assert sessions.mirror_links == {}

    @pytest.mark.asyncio
    async def test_losing_the_claim_race_reads_as_a_conflict_not_a_fault(self) -> None:
        """The precheck and the dashboard's connect endpoint hold different locks."""
        client, sessions = _Client(), _Sessions()

        def _taken(*_a, **_kw):
            raise ConversationOwnershipConflict("claimed")

        sessions.set_mirror_link = _taken  # type: ignore[assignment]
        d = _dispatcher(sessions, client, TestPressing._log())

        await self._pressed(d, client)

        assert "another session just connected here" in str(client.updated[-1][1])

    @pytest.mark.asyncio
    async def test_an_unexpected_persist_failure_still_tells_the_user(self) -> None:
        client, sessions = _Client(), _Sessions()

        def _boom(*_a, **_kw):
            raise RuntimeError("map broken")

        sessions.set_mirror_link = _boom  # type: ignore[assignment]
        d = _dispatcher(sessions, client, TestPressing._log())

        await self._pressed(d, client)

        assert "couldn't resume that session" in str(client.updated[-1][1])


class TestTheDashboardSeesTheBindingAtOnce:
    """Without a push, an open dashboard shows no "driven from" chip until something
    unrelated happens to refresh slots."""

    @pytest.mark.asyncio
    async def test_a_press_pushes_the_slot_update(self) -> None:
        pushed: list[int] = []
        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, TestPressing._log())
        d._session_resume.dashboard_state = SimpleNamespace(
            push_slots_update=lambda: pushed.append(1)
        )

        await d.handle_message(_inbound("/sessions"))
        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

        assert pushed == [1]

    @pytest.mark.asyncio
    async def test_a_failing_push_does_not_break_the_bind(self) -> None:
        """A dashboard nicety must not undo a binding the user just made."""

        def _boom() -> None:
            raise RuntimeError("no listeners")

        client, sessions = _Client(), _Sessions()
        d = _dispatcher(sessions, client, TestPressing._log())
        d._session_resume.dashboard_state = SimpleNamespace(push_slots_update=_boom)

        await d.handle_message(_inbound("/sessions"))
        await d.handle_message(_inbound("", value=_press(client.cards[0], 0)))

        assert sessions.mirror_links == {"dashboard:chat-1": ChannelLink("teams", "CONV")}


class TestTitleFallback:
    @pytest.mark.asyncio
    async def test_an_unreadable_title_falls_back_to_the_bare_key(self) -> None:
        """A bootstrapped record still has to name the conversation somehow."""

        class _Log(_ConversationLog):
            def get_metadata(self, key: str) -> dict:
                raise OSError("metadata gone")

        client, sessions = _Client(), _Sessions()
        sessions.set_mirror_link(
            "dashboard:chat-1", ChannelLink("teams", "CONV"), accepts_inbound=True
        )
        d = _dispatcher(sessions, client, _Log(_rows("Launch plan")))

        assert await d._session_resume._title_of("dashboard:chat-1") == "chat-1"


class TestSharedTitleLoader:
    """One loader serves every channel's resume adapters.

    Three adapters grew the same ``conv_log.get_metadata`` read (Discord, Teams,
    Telegram), so a fix to the unwrap or the fallback landed in one and missed
    the others. The shared helper keeps the read off-loop and the fallback
    identical, and every channel names itself so its debug line stays its own.
    """

    @staticmethod
    def _log(metadata: dict[str, dict] | None = None, *, raises: bool = False):
        class _Log:
            def get_metadata(self, key: str) -> dict:
                if raises:
                    raise OSError("metadata gone")
                return dict((metadata or {}).get(key, {}))

        return _Log()

    def test_the_stored_title_is_returned(self) -> None:
        log = self._log({"dashboard:chat-1": {"title": "Launch plan"}})
        assert core.session_title_of(log, "dashboard:chat-1") == "Launch plan"

    def test_a_blank_title_collapses_to_the_bare_key(self) -> None:
        """An EMPTY title is falsy, so the fallback names the conversation."""
        log = self._log({"dashboard:chat-1": {"title": ""}})
        assert core.session_title_of(log, "dashboard:chat-1") == "chat-1"

    def test_a_whitespace_title_is_passed_through_unchanged(self) -> None:
        """Preserved, not tidied: the shared read is not where a title is trimmed.

        A whitespace-only title is truthy, so the pre-consolidation copies
        returned it verbatim. Trimming here would be a behaviour change riding
        along with a refactor.
        """
        log = self._log({"dashboard:chat-1": {"title": "   "}})
        assert core.session_title_of(log, "dashboard:chat-1") == "   "

    def test_a_key_with_no_dashboard_prefix_survives_the_fallback(self) -> None:
        assert core.session_title_of(self._log(), "slack:legacy") == "slack:legacy"

    def test_an_absent_conv_log_falls_back_rather_than_raising(self) -> None:
        assert core.session_title_of(None, "dashboard:chat-1") == "chat-1"

    def test_an_unreadable_log_falls_back_rather_than_raising(self) -> None:
        assert core.session_title_of(self._log(raises=True), "dashboard:chat-1") == "chat-1"

    def test_a_missing_title_key_falls_back(self) -> None:
        log = self._log({"dashboard:chat-1": {"agent": "writer"}})
        assert core.session_title_of(log, "dashboard:chat-1") == "chat-1"

    def test_the_read_is_blocking_so_async_callers_own_the_offload(self) -> None:
        """Metadata access blocks; the helper must stay sync for ``to_thread``.

        Same contract as ``persisted_session_agent``: a coroutine here would
        force every caller onto the loop thread.
        """
        assert not asyncio.iscoroutinefunction(core.session_title_of)
