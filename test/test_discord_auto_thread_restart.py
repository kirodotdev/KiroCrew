"""An ``auto_thread`` thread keeps working after the gateway restarts.

The runtime grant lives in memory only, so a restarted process holds an empty
thread roster. These tests build exactly that state (an allowed channel, no
allowed threads) and check that a thread is re-admitted only when Discord
itself says this bot opened it under a currently allowed channel, and that
every other thread stays refused.
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


def _restarted(
    *, allowed_channels: tuple[str, ...] = ("c1",)
) -> tuple[DiscordTransport, list[InboundMessage], FakeClient, list[str]]:
    """A transport as a fresh process builds it: no runtime-promoted threads."""
    dispatched: list[InboundMessage] = []
    registered: list[str] = []

    async def _dispatch(m: InboundMessage) -> None:
        dispatched.append(m)

    client = FakeClient()
    t = DiscordTransport(
        client,  # type: ignore[arg-type]
        allowed_user_ids=["u1"],
        allowed_channel_ids=list(allowed_channels),
        on_thread_created=registered.append,
        dispatch=_dispatch,
    )
    return t, dispatched, client, registered


def _in_thread(user: str = "u1", thread: str = "t9") -> DiscordInbound:
    return DiscordInbound(channel_id=thread, user_id=user, text="still there?", guild_id="g1")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.discord.transport.sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: seen.append(kw)),
    )
    return seen


class TestReceiveAfterRestart:
    @pytest.mark.asyncio
    async def test_bot_opened_thread_under_allowed_channel_answers_again(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _restarted()
        client.thread_channels.add("t9")
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")

        await t.receive(_in_thread())

        assert [m.thread_id for m in dispatched] == ["t9"]
        assert registered == ["t9"]
        assert [(e["outcome"], e["resources"]) for e in events] == [
            ("thread_reauthorized", "channel=c1,thread=t9")
        ]
        # The second message rides the restored grant with no further lookup.
        await t.receive(_in_thread())
        assert len(dispatched) == 2
        assert client.origin_lookups == ["t9"]

    @pytest.mark.asyncio
    async def test_restored_thread_is_sendable_again(self) -> None:
        t, _dispatched, client, _registered = _restarted()
        client.thread_channels.add("t9")
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")
        assert t.may_send_to("t9") is False
        await t.receive(_in_thread())
        assert t.may_send_to("t9") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "origin,label",
        [
            (ThreadOrigin(11, "c1", "someone-else"), "opened by a person, not this bot"),
            (ThreadOrigin(11, "c2", "bot"), "parent channel not allowed"),
            (ThreadOrigin(12, "c1", "bot"), "private thread auto_thread never makes"),
            (None, "lookup failed or not a thread"),
        ],
    )
    async def test_every_other_thread_stays_refused(
        self, events: list[dict[str, Any]], origin: ThreadOrigin | None, label: str
    ) -> None:
        t, dispatched, client, registered = _restarted()
        client.thread_channels.add("t9")
        if origin is not None:
            client.thread_origins["t9"] = origin

        await t.receive(_in_thread())

        assert dispatched == [], label
        assert registered == [], label
        assert t.may_send_to("t9") is False, label
        assert [e["outcome"] for e in events] == ["denied_unapproved_thread"], label

    @pytest.mark.asyncio
    async def test_unknown_bot_identity_fails_closed(self, events: list[dict[str, Any]]) -> None:
        t, dispatched, client, _registered = _restarted()
        client.bot_user_id = ""
        client.thread_channels.add("t9")
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "")

        await t.receive(_in_thread())

        assert dispatched == []
        assert client.origin_lookups == []

    @pytest.mark.asyncio
    async def test_no_allowed_channels_means_no_lookup(self, events: list[dict[str, Any]]) -> None:
        t, dispatched, client, _registered = _restarted(allowed_channels=())
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")

        await t.receive(_in_thread())

        assert dispatched == []
        assert client.origin_lookups == []

    @pytest.mark.asyncio
    async def test_unapproved_sender_costs_no_lookup(self, events: list[dict[str, Any]]) -> None:
        t, dispatched, client, _registered = _restarted()
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")

        await t.receive(_in_thread(user="stranger"))

        assert dispatched == []
        assert client.origin_lookups == []
        assert events == []

    @pytest.mark.asyncio
    async def test_parent_removed_during_lookup_is_honoured(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _restarted()
        client.thread_channels.add("t9")

        async def _origin_then_reload(thread_id: str) -> ThreadOrigin:
            t.reconfigure(
                SimpleNamespace(
                    allowed_user_ids=["u1"],
                    allowed_channel_ids=[],
                    allowed_thread_ids=[],
                    auto_thread=True,
                )
            )
            return ThreadOrigin(11, "c1", "bot")

        client.thread_origin = _origin_then_reload  # type: ignore[method-assign]
        await t.receive(_in_thread())

        assert dispatched == []
        assert registered == []

    @pytest.mark.asyncio
    async def test_sender_removed_during_lookup_grants_nothing(
        self, events: list[dict[str, Any]]
    ) -> None:
        t, dispatched, client, registered = _restarted()
        client.thread_channels.add("t9")

        async def _origin_then_revoke(thread_id: str) -> ThreadOrigin:
            t.reconfigure(
                SimpleNamespace(
                    allowed_user_ids=[],
                    allowed_channel_ids=["c1"],
                    allowed_thread_ids=[],
                    auto_thread=True,
                )
            )
            return ThreadOrigin(11, "c1", "bot")

        client.thread_origin = _origin_then_revoke  # type: ignore[method-assign]

        assert await t.readmit_own_thread("t9", "u1") is False
        assert registered == []
        assert t.may_send_to("t9") is False
        assert "thread_reauthorized" not in [e["outcome"] for e in events]

    @pytest.mark.asyncio
    async def test_removing_the_parent_takes_effect_at_the_next_restart(
        self, events: list[dict[str, Any]]
    ) -> None:
        """What discord-integration.md tells an operator about revoking a channel.

        A live reload keeps every runtime-promoted thread (``reconfigure`` unions
        them back), so a re-admitted thread keeps answering after its parent is
        removed. Only the next restart, which starts from an empty roster and
        re-checks the parent, stops it. The guide says exactly this; if either
        half changes, the guide is wrong again.
        """
        t, dispatched, client, _registered = _restarted()
        client.thread_channels.add("t9")
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")
        await t.receive(_in_thread())
        assert len(dispatched) == 1

        removed = SimpleNamespace(
            allowed_user_ids=["u1"],
            allowed_channel_ids=[],
            allowed_thread_ids=[],
            auto_thread=True,
        )
        t.reconfigure(removed)
        await t.receive(_in_thread())
        assert len(dispatched) == 2, "a live reload keeps the promoted thread"
        assert t.may_send_to("t9") is True

        fresh, fresh_dispatched, fresh_client, _ = _restarted(allowed_channels=())
        fresh_client.thread_channels.add("t9")
        fresh_client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")
        await fresh.receive(_in_thread())
        assert fresh_dispatched == [], "the next restart refuses it"
        assert fresh.may_send_to("t9") is False

    @pytest.mark.asyncio
    async def test_thread_removed_from_config_is_not_readmitted(
        self, events: list[dict[str, Any]]
    ) -> None:
        """A bot-opened thread an operator lists and then removes stays revoked.

        ``reconfigure`` drops a removed ``allowed_thread_ids`` entry; the
        re-admission path must not add it back on the next message, even
        though Discord still reports this bot opened it under an allowed
        channel. Listing it again lifts the revocation.
        """
        t, dispatched, client, registered = _restarted()
        listed = SimpleNamespace(
            allowed_user_ids=["u1"],
            allowed_channel_ids=["c1"],
            allowed_thread_ids=["t9"],
            auto_thread=True,
        )
        t.reconfigure(listed)
        client.thread_channels.add("t9")
        client.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")
        await t.receive(_in_thread())
        assert len(dispatched) == 1

        t.reconfigure(
            SimpleNamespace(
                allowed_user_ids=["u1"],
                allowed_channel_ids=["c1"],
                allowed_thread_ids=[],
                auto_thread=True,
            )
        )
        await t.receive(_in_thread())
        assert len(dispatched) == 1, "a revoked thread must not answer again"
        assert await t.readmit_own_thread("t9", "u1") is False
        assert t.may_send_to("t9") is False
        assert registered == []
        assert client.origin_lookups == []
        assert "thread_reauthorized" not in [e["outcome"] for e in events]

        t.reconfigure(listed)
        await t.receive(_in_thread())
        assert len(dispatched) == 2, "listing it again restores it"

    @pytest.mark.asyncio
    async def test_thread_revoked_during_lookup_grants_nothing(
        self, events: list[dict[str, Any]]
    ) -> None:
        """Two reloads land while the lookup is in flight: one lists the
        thread, the next removes it. The removal is the operator's last word,
        so the answer that arrives afterwards must not re-admit it."""
        t, _dispatched, client, registered = _restarted()

        def _reload(threads: list[str]) -> None:
            t.reconfigure(
                SimpleNamespace(
                    allowed_user_ids=["u1"],
                    allowed_channel_ids=["c1"],
                    allowed_thread_ids=threads,
                    auto_thread=True,
                )
            )

        async def _origin_then_list_and_revoke(thread_id: str) -> ThreadOrigin:
            _reload(["t9"])
            _reload([])
            return ThreadOrigin(11, "c1", "bot")

        client.thread_origin = _origin_then_list_and_revoke  # type: ignore[method-assign]
        assert await t.readmit_own_thread("t9", "u1") is False
        assert registered == []
        assert t.may_send_to("t9") is False
        assert "thread_reauthorized" not in [e["outcome"] for e in events]


class TestInteractionAfterRestart:
    @pytest.mark.asyncio
    async def test_slash_command_in_restored_thread_is_actionable(self) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        t = DiscordTransport(
            cli,  # type: ignore[arg-type]
            allowed_user_ids=["u1"],
            allowed_channel_ids=["c1"],
            on_thread_created=d.register_allowed_thread,
        )
        d.transport = t
        cli.thread_channels.add("t9")
        cli.thread_origins["t9"] = ThreadOrigin(11, "c1", "bot")

        assert await d._interaction_actionable("u1", "t9") is True
        assert "t9" in d._allowed_threads

    @pytest.mark.asyncio
    async def test_slash_command_in_foreign_thread_stays_refused(self) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        d.transport = DiscordTransport(
            cli,  # type: ignore[arg-type]
            allowed_user_ids=["u1"],
            allowed_channel_ids=["c1"],
            on_thread_created=d.register_allowed_thread,
        )
        cli.thread_channels.add("t9")
        cli.thread_origins["t9"] = ThreadOrigin(11, "c1", "someone-else")

        assert await d._interaction_actionable("u1", "t9") is False
        assert "t9" not in d._allowed_threads


class TestClientThreadOrigin:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,expected",
        [
            (
                {"type": 11, "parent_id": "c1", "owner_id": "bot"},
                ThreadOrigin(11, "c1", "bot"),
            ),
            ({"type": 0, "parent_id": "cat", "owner_id": "x"}, None),
            ({"type": 11, "parent_id": "c1"}, None),
            ({"type": 11, "owner_id": "bot"}, None),
            (None, None),
            ({"type": "11", "parent_id": "c1", "owner_id": "bot"}, None),
        ],
    )
    async def test_reads_origin_and_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any, expected: ThreadOrigin | None
    ) -> None:
        from kiro_crew.discord.client import DiscordClient

        client = DiscordClient(token="bot-secret", proxy="http://proxy.invalid:8080")
        calls: list[str] = []

        async def _api_json(method: str, path: str, body: Any, **kw: Any) -> Any:
            # The admission lookup is the one route that opts out of the
            # ladder's roster re-read; its caller re-reads the rosters itself.
            assert kw.get("recheck_destination") is False
            calls.append(f"{method} {path}")
            return SimpleNamespace(data=payload)

        monkeypatch.setattr(client, "api_json", _api_json)
        assert await client.thread_origin("t9") == expected
        # Never served from a cache: it is an authorization input.
        await client.thread_origin("t9")
        assert calls == ["GET /channels/t9", "GET /channels/t9"]
