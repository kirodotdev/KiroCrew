"""Discord's owner setting, ``discord.owner_id``.

It names the owner among the allowed users; unset, the only allowed user is the
owner and several leave nobody. Every owner reader (session resume, session
control's owner DM, the proactive owner DM, bridged-approval replies, the owner
notice, the owner-DM reopen) gets the same answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeCtx, FakeSessions, _cfg, _prime_live

from kiro_crew.config.section_builders import _build_discord_config
from kiro_crew.discord.transport import DiscordTransport, resolve_owner_id
from kiro_crew.discord.transport_dispatch import DiscordDispatcher


class TestOwnerSetting:
    def test_unset_keeps_the_single_allowed_user_rule(self) -> None:
        assert resolve_owner_id("", {"111"}) == "111"
        assert resolve_owner_id("", {"111", "222"}) == ""
        assert resolve_owner_id("", set()) == ""

    def test_a_named_owner_counts_only_while_allowed(self) -> None:
        assert resolve_owner_id("111", {"111", "222"}) == "111"
        # Naming an owner admits nobody: one missing from the roster is no owner,
        # and the sole allowed user does not take its place.
        assert resolve_owner_id("333", {"111"}) == ""

    def test_the_loader_keeps_one_snowflake_and_drops_anything_else(self) -> None:
        assert _build_discord_config({}).owner_id == ""
        assert _build_discord_config({"owner_id": " 111 "}).owner_id == "111"
        assert _build_discord_config({"owner_id": 111}).owner_id == "111"
        assert _build_discord_config({"owner_id": "me"}).owner_id == ""
        assert _build_discord_config({"owner_id": ["111"]}).owner_id == ""

    def test_the_dispatcher_reads_the_owner_at_boot_and_on_reload(self) -> None:
        cfg = _cfg()
        cfg.discord.owner_id = "111"
        _prime_live(cfg)
        d = DiscordDispatcher(
            sessions=FakeSessions(),  # type: ignore[arg-type]
            ctx_builder=FakeCtx(),  # type: ignore[arg-type]
            cfg=cfg,
            allowed_user_ids={"111", "222"},
        )
        assert d._session_resume.is_owner("111")
        assert not d._session_resume.is_owner("222")

        d.reconfigure(
            SimpleNamespace(allowed_user_ids=["111", "222"], allowed_thread_ids=[], owner_id="222")
        )
        assert d._session_resume.is_owner("222")
        assert not d._session_resume.is_owner("111")

        # Removing the named owner from the roster leaves nobody, at once.
        d.reconfigure(
            SimpleNamespace(allowed_user_ids=["111"], allowed_thread_ids=[], owner_id="222")
        )
        assert not d._session_resume.is_owner("111")
        assert not d._session_resume.is_owner("222")

        # Clearing the setting brings the single-user rule back.
        d.reconfigure(SimpleNamespace(allowed_user_ids=["111"], allowed_thread_ids=[], owner_id=""))
        assert d._session_resume.is_owner("111")


def _transport(allowed: list[str], owner_id: str = "") -> DiscordTransport:
    return DiscordTransport(
        SimpleNamespace(),  # type: ignore[arg-type]
        allowed_user_ids=allowed,
        owner_id=owner_id,
    )


class TestSharedOwnerRule:
    """Every owner reader goes through ``owner_direct_target``; Discord's setting wins there."""

    def test_no_setting_is_the_inference_unchanged(self) -> None:
        from kiro_crew.messaging.transport import owner_direct_target, sole_direct_target

        for allowed in (["111"], ["111", "222"], []):
            transport = _transport(allowed)
            targets = transport.configured_targets()
            assert transport.explicit_owner_target() is None
            assert owner_direct_target(transport, targets) == sole_direct_target(targets)
        # A transport with no hook, or a double whose hook answers a non-string.
        targets = _transport(["111"]).configured_targets()
        assert owner_direct_target(SimpleNamespace(), targets) == "user:111"
        odd = SimpleNamespace(explicit_owner_target=lambda: object())
        assert owner_direct_target(odd, targets) == "user:111"

    def test_a_named_owner_wins_only_as_an_available_configured_dm(self) -> None:
        from kiro_crew.messaging.transport import ConfiguredChannelTarget, owner_direct_target

        transport = _transport(["111", "222"], owner_id="111")
        assert transport.explicit_owner_target() == "user:111"
        assert owner_direct_target(transport, transport.configured_targets()) == "user:111"
        # Not on the roster: nobody, and the sole allowed user does not take its place.
        lone = _transport(["222"], owner_id="111")
        assert lone.explicit_owner_target() == ""
        assert owner_direct_target(lone, lone.configured_targets()) == ""
        named = SimpleNamespace(explicit_owner_target=lambda: "user:111")
        for target in (
            ConfiguredChannelTarget("user:111", "x", available=False),
            ConfiguredChannelTarget("user:111", "x", learned=True),
            ConfiguredChannelTarget("thread:111", "x"),
        ):
            assert owner_direct_target(named, [target]) == ""
        thread_named = SimpleNamespace(explicit_owner_target=lambda: "thread:9")
        assert owner_direct_target(thread_named, [ConfiguredChannelTarget("thread:9", "x")]) == ""

    def test_the_transport_follows_a_reload(self) -> None:
        transport = _transport(["111", "222"])
        transport.reconfigure(
            SimpleNamespace(allowed_user_ids=["111", "222"], allowed_channel_ids=[], owner_id="222")
        )
        assert transport.explicit_owner_target() == "user:222"
        transport.reconfigure(
            SimpleNamespace(allowed_user_ids=["111", "222"], allowed_channel_ids=[], owner_id="")
        )
        assert transport.explicit_owner_target() is None

    def test_session_control_owner_dm_and_bridge_replies_name_the_set_owner(self) -> None:
        from kiro_crew.dashboard.handlers.messaging import _owner_dm_target
        from kiro_crew.dashboard.session_control_parts.target_authorization import _sole_owner
        from kiro_crew.notifications.bridge_transport import reply_owner

        named = _transport(["111", "222"], owner_id="111")
        unnamed = _transport(["111", "222"])
        for transport, dm, owner in ((named, "user:111", "111"), (unnamed, "", "")):
            state = SimpleNamespace(get_channel_transport=lambda _s, t=transport: t)
            assert _sole_owner(state, "discord")[1] == dm  # type: ignore[arg-type]
            assert _owner_dm_target(transport) == dm
            assert reply_owner(state, "discord") == owner


class TestOwnerNotice:
    """The cross-channel owner notice honours an explicit owner it cannot name."""

    @staticmethod
    async def _notice(transport: DiscordTransport, *, reload_to: Any = None) -> list[str]:
        """Run the notice; *reload_to* is a section applied while the target resolves."""
        from kiro_crew.dashboard import server

        sent: list[str] = []

        async def _resolve(target_id: str) -> tuple[str, None]:
            if reload_to is not None:
                transport.reconfigure(reload_to)
            return f"dm-{target_id}", None

        async def _send(conversation_id: str, text: str, thread_id: Any = None) -> str:
            sent.append(conversation_id)
            return "m1"

        transport.resolve_configured_target = _resolve  # type: ignore[method-assign]
        transport.send_message = _send  # type: ignore[method-assign]
        state = SimpleNamespace(channel_transports={"discord": transport})
        await server._notify_owner_channels(state, "override expired")  # type: ignore[arg-type]
        return sent

    @pytest.mark.asyncio
    async def test_a_named_owner_off_the_roster_gets_no_stand_in(self) -> None:
        assert await self._notice(_transport(["222"], owner_id="111")) == []

    @pytest.mark.asyncio
    async def test_the_named_or_sole_owner_is_reached(self) -> None:
        assert await self._notice(_transport(["222"], owner_id="222")) == ["dm-user:222"]
        assert await self._notice(_transport(["222"])) == ["dm-user:222"]

    @pytest.mark.asyncio
    async def test_an_owner_renamed_while_the_target_resolves_gets_nothing(self) -> None:
        """The old owner is still allowed, but a reload named someone else mid-send."""
        moved = SimpleNamespace(allowed_user_ids=["222"], allowed_channel_ids=[], owner_id="333")
        assert await self._notice(_transport(["222"], owner_id="222"), reload_to=moved) == []
