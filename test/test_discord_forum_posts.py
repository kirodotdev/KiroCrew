"""A post in an allow-listed forum channel is answered as its own session (B4).

A forum post's messages carry the post's thread id, never the forum's, and a
person opens the post, not this bot. So neither the allowed-channel branch nor
the bot-opened-thread re-admission ever matched one, and every post was audited
``denied_unapproved_thread``. A post is admitted when Discord reports its parent
as a forum in ``allowed_channel_ids``, its opener is an allowed user and
``auto_thread`` is on; the rosters and the switch are read again after the
lookups.

Decided: a post an OUTSIDER opened stays refused even when an allowed user
replies in it, matching the opener rule (everyone who can see the post reads the
replies, and the opener chose that audience).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeClient, _dispatcher

from kiro_crew.discord.client import DiscordInbound, ThreadOrigin
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.messaging.transport import InboundMessage

FORUM = "f1"
POST = "p9"


class ForumFakeClient(FakeClient):
    """``FakeClient`` plus a forum-type lookup that records each call."""

    def __init__(self) -> None:
        super().__init__()
        self.forum_channels: set[str] = set()
        self.forum_lookups: list[str] = []

    async def is_forum_channel(self, channel_id: str) -> bool:
        self.forum_lookups.append(channel_id)
        return channel_id in self.forum_channels


def _transport(
    *,
    users: tuple[str, ...] = ("u1", "u2"),
    channels: tuple[str, ...] = (FORUM,),
    auto_thread: bool = True,
) -> tuple[DiscordTransport, list[InboundMessage], ForumFakeClient, list[str]]:
    dispatched: list[InboundMessage] = []
    registered: list[str] = []

    async def _dispatch(m: InboundMessage) -> None:
        dispatched.append(m)

    client = ForumFakeClient()
    client.forum_channels.add(FORUM)
    client.thread_channels.add(POST)
    t = DiscordTransport(
        client,  # type: ignore[arg-type]
        allowed_user_ids=list(users),
        allowed_channel_ids=list(channels),
        on_thread_created=registered.append,
        dispatch=_dispatch,
        auto_thread=auto_thread,
    )
    return t, dispatched, client, registered


def _section(
    users: list[str],
    channels: list[str],
    threads: list[str] | None = None,
    auto_thread: bool = True,
) -> Any:
    return SimpleNamespace(
        allowed_user_ids=users,
        allowed_channel_ids=channels,
        allowed_thread_ids=threads or [],
        auto_thread=auto_thread,
    )


def _in_post(user: str = "u1", post: str = POST) -> DiscordInbound:
    return DiscordInbound(
        channel_id=post, user_id=user, text="how do I deploy?", guild_id="g1", message_id=post
    )


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.discord.transport.sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: seen.append(kw)),
    )
    return seen


def _outcomes(events: list[dict[str, Any]]) -> list[str]:
    return [e["outcome"] for e in events]


class TestForumPostAdmitted:
    @pytest.mark.asyncio
    async def test_allowed_users_post_in_allowed_forum_is_answered(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")

        await t.receive(_in_post())

        assert [(m.thread_id, m.conversation_id) for m in dispatched] == [(POST, POST)]
        assert registered == [POST]
        assert [(e["outcome"], e["operation"], e["resources"]) for e in events] == [
            (
                "forum_post_authorized",
                "discord_transport.forum_post",
                f"channel={FORUM},thread={POST}",
            )
        ]
        assert t.may_send_to(POST) is True
        # Follow-ups ride the grant with no further lookup.
        await t.receive(_in_post())
        assert len(dispatched) == 2
        assert client.origin_lookups == [POST]
        assert client.forum_lookups == [FORUM]

    @pytest.mark.asyncio
    async def test_another_allowed_user_may_open_the_conversation(
        self, events: list[dict[str, Any]]
    ) -> None:
        """The opener and the first sender need not be the same allowed user."""
        t, dispatched, client, _registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u2")

        await t.receive(_in_post(user="u1"))

        assert len(dispatched) == 1
        assert _outcomes(events) == ["forum_post_authorized"]

    @pytest.mark.asyncio
    async def test_slash_command_in_allowed_post_is_actionable(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        cli = ForumFakeClient()
        cli.forum_channels.add(FORUM)
        cli.thread_channels.add(POST)
        cli.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")
        d.client = cli  # type: ignore[assignment]
        d.transport = DiscordTransport(
            cli,  # type: ignore[arg-type]
            allowed_user_ids=["u1"],
            allowed_channel_ids=[FORUM],
            on_thread_created=d.register_allowed_thread,
        )

        assert await d._interaction_actionable("u1", POST) is True
        assert POST in d._allowed_threads


class TestForumPostRefused:
    @pytest.mark.asyncio
    async def test_outsiders_post_is_refused_even_when_an_allowed_user_replies(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "outsider")

        await t.receive(_in_post(user="u1"))

        assert dispatched == []
        assert registered == []
        assert t.may_send_to(POST) is False
        assert _outcomes(events) == ["denied_unapproved_thread"]

    @pytest.mark.asyncio
    async def test_outsiders_own_message_in_their_post_costs_no_lookup(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, _registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "outsider")

        await t.receive(_in_post(user="outsider"))

        assert dispatched == []
        assert client.origin_lookups == []
        assert events == []

    @pytest.mark.asyncio
    async def test_forum_not_allow_listed_is_refused(self, events: list[dict[str, Any]]) -> None:
        t, dispatched, client, registered = _transport(channels=("c-other",))
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")

        await t.receive(_in_post())

        assert dispatched == []
        assert registered == []
        assert client.forum_lookups == []
        assert _outcomes(events) == ["denied_unapproved_thread"]

    @pytest.mark.asyncio
    async def test_persons_thread_under_allowed_text_channel_behaves_as_before(
        self, events: list[dict[str, Any]]
    ) -> None:
        """Same id shapes, but Discord reports the parent as a text channel."""
        t, dispatched, client, registered = _transport(channels=("c1",))
        client.thread_origins[POST] = ThreadOrigin(11, "c1", "u1")

        await t.receive(_in_post())

        assert dispatched == []
        assert registered == []
        assert client.forum_lookups == ["c1"]
        assert _outcomes(events) == ["denied_unapproved_thread"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", [10, 12])
    async def test_only_a_public_thread_is_a_post(
        self, events: list[dict[str, Any]], kind: int
    ) -> None:
        t, dispatched, client, _registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(kind, FORUM, "u1")

        await t.receive(_in_post())

        assert dispatched == []
        assert client.forum_lookups == []

    @pytest.mark.asyncio
    async def test_failed_lookup_is_refused(self, events: list[dict[str, Any]]) -> None:
        t, dispatched, client, _registered = _transport()

        await t.receive(_in_post())

        assert dispatched == []
        assert _outcomes(events) == ["denied_unapproved_thread"]

    @pytest.mark.asyncio
    async def test_opener_removed_during_origin_lookup_is_refused(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _transport()

        async def _origin_then_revoke(thread_id: str) -> ThreadOrigin:
            t.reconfigure(_section(["u2"], [FORUM]))
            return ThreadOrigin(11, FORUM, "u1")

        client.thread_origin = _origin_then_revoke  # type: ignore[method-assign]

        assert await t.readmit_own_thread(POST, "u2") is False
        assert registered == []
        assert t.may_send_to(POST) is False
        assert "forum_post_authorized" not in _outcomes(events)

    @pytest.mark.parametrize(
        "after",
        [
            _section(["u2"], [FORUM]),  # the opener is withdrawn
            _section(["u1"], [FORUM]),  # the sender is withdrawn
            _section(["u1", "u2"], []),  # the forum is withdrawn
            _section(["u1", "u2"], [FORUM], [POST]),  # listed then removed: revoked
        ],
        ids=["opener", "sender", "forum", "thread"],
    )
    @pytest.mark.asyncio
    async def test_roster_change_during_forum_lookup_is_refused(
        self, events: list[dict[str, Any]], after: Any
    ) -> None:
        t, dispatched, client, registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")
        revoking_thread = POST in after.allowed_thread_ids

        async def _forum_then_reload(channel_id: str) -> bool:
            t.reconfigure(after)
            if revoking_thread:
                t.reconfigure(_section(["u1", "u2"], [FORUM]))
            return True

        client.is_forum_channel = _forum_then_reload  # type: ignore[method-assign]

        await t.receive(_in_post(user="u2"))

        assert dispatched == []
        assert registered == []
        assert POST not in t._allowed_threads
        assert "forum_post_authorized" not in _outcomes(events)

    @pytest.mark.asyncio
    async def test_post_is_refused_while_auto_thread_is_off(
        self, events: list[dict[str, Any]]
    ) -> None:
        """With ``auto_thread`` off an allowed channel gets no reply; a forum too."""
        t, dispatched, client, registered = _transport(auto_thread=False)
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")

        await t.receive(_in_post())

        assert dispatched == []
        assert registered == []
        assert t.may_send_to(POST) is False
        assert "forum_post_authorized" not in _outcomes(events)

    @pytest.mark.asyncio
    async def test_auto_thread_turned_off_during_forum_lookup_is_refused(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")

        async def _forum_then_reload(channel_id: str) -> bool:
            t.reconfigure(_section(["u1", "u2"], [FORUM], auto_thread=False))
            return True

        client.is_forum_channel = _forum_then_reload  # type: ignore[method-assign]

        await t.receive(_in_post(user="u2"))

        assert dispatched == []
        assert registered == []
        assert POST not in t._allowed_threads
        assert "forum_post_authorized" not in _outcomes(events)

    @pytest.mark.asyncio
    async def test_revoked_post_is_not_readmitted(self, events: list[dict[str, Any]]) -> None:
        t, dispatched, client, _registered = _transport()
        client.thread_origins[POST] = ThreadOrigin(11, FORUM, "u1")
        t.reconfigure(_section(["u1", "u2"], [FORUM], [POST]))
        await t.receive(_in_post())
        assert len(dispatched) == 1

        t.reconfigure(_section(["u1", "u2"], [FORUM]))
        await t.receive(_in_post())

        assert len(dispatched) == 1
        assert t.may_send_to(POST) is False
        assert "forum_post_authorized" not in _outcomes(events)


class TestClientIsForumChannel:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"type": 15}, True),
            ({"type": 16}, True),
            ({"type": 0}, False),
            ({"type": 11}, False),
            ({"type": "15"}, False),
            ({}, False),
            (None, False),
        ],
    )
    async def test_reads_type_and_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any, expected: bool
    ) -> None:
        from kiro_crew.discord.client import DiscordClient

        client = DiscordClient(token="bot-secret", proxy="http://proxy.invalid:8080")
        calls: list[str] = []

        async def _api(method: str, path: str, body: Any, **kw: Any) -> Any:
            calls.append(f"{method} {path}")
            return payload

        monkeypatch.setattr(client, "_api", _api)
        assert await client.is_forum_channel(FORUM) is expected
        await client.is_forum_channel(FORUM)
        # A confirmed type is cached (Discord cannot convert a forum); a failed
        # lookup is asked again.
        resolved = isinstance(payload, dict) and isinstance(payload.get("type"), int)
        assert len(calls) == (1 if resolved else 2)
        assert calls[0] == f"GET /channels/{FORUM}"
