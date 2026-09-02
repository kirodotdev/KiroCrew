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
from types import SimpleNamespace
from typing import Any

import pytest

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
        self.searched: list[str] = []

    def list_sessions(self) -> list[dict]:
        return list(self.rows)

    def search_sessions(self, query: str, limit: int) -> list[dict]:
        self.searched.append(query)
        needle = query.casefold()
        return [r for r in self.rows if needle in str(r.get("title", "")).casefold()]

    def get_metadata(self, key: str) -> dict:
        for row in self.rows:
            if str(row.get("key", "")).endswith(key.removeprefix("dashboard:")):
                return {"title": row.get("title", "")}
        return {}

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

    # -- mirror bindings --
    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        return [
            key
            for key, bound in self.mirror_links.items()
            if bound == link and (not inbound_only or key in self.inbound_keys)
        ]

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

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

    def max_generation(self, bucket: str) -> int:
        return -1

    def clear_queue(self, key) -> None:
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
        assert "No dashboard sessions matched" in client.sent[-1]

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

        assert client.sent[-1] == "No recent dashboard sessions."

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
