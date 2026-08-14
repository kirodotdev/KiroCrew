"""Contract tests for the channel registry and the §9 address parser (PR ③).

Two contracts pinned here, deliberately in one file because they meet at the
same invariant: the registry's ``channel_type`` IS the session key's first
segment IS the governance member id. If any of the three drifts, channels
become unaddressable or ungoverned.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.messaging import registry
from kiro_crew.messaging.link import (
    CHANNEL_SESSION_NAMESPACES,
    assert_colon_free,
    build_dm_session_key,
    parse_session_key,
)
from kiro_crew.messaging.registry import ChannelDescriptor


class TestRegistryRoster:
    def test_the_seven_builtin_channels_are_registered(self) -> None:
        members = registry.governed_members(builtin_channel_descriptors())
        assert set(members) == {
            "slack",
            "discord",
            "telegram",
            "webex",
            "wecom",
            "teams",
            "weixin",
        }

    def test_channel_type_matches_each_transport_class(self) -> None:
        """The descriptor id and the transport's channel_type must be ONE fact."""
        from kiro_crew.discord.transport import DiscordTransport
        from kiro_crew.slack.transport import SlackTransport
        from kiro_crew.teams.transport import TeamsTransport
        from kiro_crew.telegram.transport import TelegramTransport
        from kiro_crew.webex.transport import WebexTransport
        from kiro_crew.wecom.transport import WeComTransport
        from kiro_crew.weixin.transport import WeixinTransport

        transports = {
            t.channel_type: t
            for t in (
                SlackTransport,
                DiscordTransport,
                TelegramTransport,
                WebexTransport,
                WeComTransport,
                TeamsTransport,
                WeixinTransport,
            )
        }
        for desc in builtin_channel_descriptors():
            assert desc.channel_type in transports, desc.channel_type

    def test_every_descriptor_surface_is_an_addressable_namespace(self) -> None:
        """channel_type must be a session-key namespace, or the channel's
        sessions are invisible to is_channel_session_key and the sidebar."""
        for desc in builtin_channel_descriptors():
            assert desc.channel_type in CHANNEL_SESSION_NAMESPACES, desc.channel_type

    def test_slack_is_governed_but_not_boot_managed(self) -> None:
        """Slack's lifecycle is host-managed (_connect_slack): a governance
        deny must DROP its socket client, which a skipped start cannot do."""
        descs = builtin_channel_descriptors()
        by_type = {d.channel_type: d for d in descs}
        assert by_type["slack"].start is None
        assert "slack" not in {d.channel_type for d in registry.bootable(descs)}
        assert len(registry.bootable(descs)) == 6


class TestRegistryBootLoop:
    def _orch(self) -> Any:
        class _Orch:
            pass

        return _Orch()

    def test_only_permitted_channels_start_and_handles_are_kept(self) -> None:
        started: list[str] = []

        async def make_start(name: str):
            async def start(orch: Any) -> Any:
                started.append(name)
                return f"{name}-client"

            return start

        async def go() -> None:
            descs = (
                ChannelDescriptor("alpha", start=await make_start("alpha")),
                ChannelDescriptor("beta", start=await make_start("beta")),
                ChannelDescriptor("host", start=None),
            )
            orch = self._orch()
            handles = await registry.start_channels(
                orch, descs, {"alpha": True, "beta": False}
            )
            assert started == ["alpha"], "beta denied, host not bootable"
            assert handles == {"alpha": "alpha-client"}
            # legacy mirror attribute stays in sync for existing readers
            assert orch._alpha_client == "alpha-client"
            # a DENIED channel is never attempted, so its attribute is not
            # touched — same as the old hand-written if-blocks (the real
            # orchestrator declares these as None in __init__)
            assert getattr(orch, "_beta_client", None) is None

        asyncio.run(go())

    def test_a_member_absent_from_permitted_is_not_started(self) -> None:
        """Enabled-only eval: unevaluated members default to not-permitted."""

        async def start(orch: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError("must not start")

        async def go() -> None:
            handles = await registry.start_channels(
                self._orch(), (ChannelDescriptor("gamma", start=start),), {}
            )
            assert handles == {}

        asyncio.run(go())

    def test_one_failing_factory_does_not_abort_the_others(self) -> None:
        async def boom(orch: Any) -> Any:
            raise RuntimeError("factory exploded")

        async def ok(orch: Any) -> Any:
            return "ok-client"

        async def go() -> None:
            descs = (
                ChannelDescriptor("boom", start=boom),
                ChannelDescriptor("ok", start=ok),
            )
            handles = await registry.start_channels(
                self._orch(), descs, {"boom": True, "ok": True}
            )
            assert handles == {"ok": "ok-client"}

        asyncio.run(go())

    def test_shutdown_tasks_close_every_handle_and_skip_closeless(self) -> None:
        closed: list[str] = []

        class _Client:
            def __init__(self, name: str) -> None:
                self._name = name

            async def close(self) -> None:
                closed.append(self._name)

        async def go() -> None:
            tasks = registry.shutdown_tasks(
                {"a": _Client("a"), "b": _Client("b"), "odd": object()}
            )
            assert len(tasks) == 2, "the close()-less handle is skipped, not fatal"
            await asyncio.gather(*tasks)
            assert sorted(closed) == ["a", "b"]

        asyncio.run(go())


class TestSessionKeyParser:
    """§9 rule 2 grammar + the channel_type == first-segment invariant."""

    def test_round_trip_for_every_builtin_channel(self) -> None:
        for desc in builtin_channel_descriptors():
            key = build_dm_session_key(desc.channel_type, "agentA", "user1")
            parsed = parse_session_key(key)
            assert parsed is not None, key
            assert parsed.surface == desc.channel_type, (
                "channel_type MUST equal the first key segment (RFC §9)"
            )
            assert parsed.agent == "agentA"
            assert parsed.chat_type == "direct"
            assert parsed.scope == ("user1",)
            assert parsed.gen == 0
            assert parsed.bucket == key

    def test_generation_suffix_parses_and_bucket_strips_it(self) -> None:
        key = build_dm_session_key("telegram", "a", "42", gen=3)
        parsed = parse_session_key(key)
        assert parsed is not None
        assert parsed.gen == 3
        assert parsed.bucket == "telegram:a:direct:42"

    def test_forum_scope_path_keeps_hierarchy_depth(self) -> None:
        """Telegram forum comp '{chat_id}:{thread}' = TWO scope segments."""
        key = build_dm_session_key("telegram", "a", "-100123:77", chat_type="forum")
        parsed = parse_session_key(key)
        assert parsed is not None
        assert parsed.scope == ("-100123", "77")
        assert parsed.bucket == key

    @pytest.mark.parametrize(
        "legacy",
        [
            "1723456789.123456",  # bare Slack thread_ts
            "slack:1723456789.123456",  # two-segment slack key
            "dashboard:dashboard_5",  # dashboard slot key
            "channel:room1:agentA",  # app-platform Channel feature prefix
            "",
        ],
    )
    def test_legacy_shapes_return_none_not_a_wrong_parse(self, legacy: str) -> None:
        assert parse_session_key(legacy) is None

    def test_an_unknown_surface_does_not_parse(self) -> None:
        assert parse_session_key("smoke-signal:a:direct:u") is None

    def test_empty_segments_are_malformed(self) -> None:
        assert parse_session_key("telegram:a::u") is None

    def test_builders_reject_colons_in_single_segments(self) -> None:
        with pytest.raises(ValueError):
            build_dm_session_key("tele:gram", "a", "u")
        with pytest.raises(ValueError):
            build_dm_session_key("telegram", "a:b", "u")
        with pytest.raises(ValueError):
            assert_colon_free("x:y", what="test")

    def test_empty_scope_subsegment_is_rejected_at_build(self) -> None:
        with pytest.raises(ValueError):
            build_dm_session_key("telegram", "a", "123::77", chat_type="forum")
