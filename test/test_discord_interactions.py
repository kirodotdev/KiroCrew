"""Discord interactions and the command surfaces the dispatcher answers.

Button interactions, the ``!`` and ``/`` command surfaces that share
one handler table, the model picker and ``!agent``, refusals that say why,
context-threshold notices, the operator's process-wide permission grant, and
that the dispatcher feeds every render toggle and renderer argument per turn.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import FakeProvider, _dispatcher, _inbound, _inbound_with_id, _live_discord

import kiro_crew.discord.transport_dispatch as td_mod
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.discord.client import DiscordInteraction
from kiro_crew.discord.commands import COMMAND_SPEC, application_command_payload, parse_command
from kiro_crew.discord.renderer import DiscordApprovalDecider, session_provenance_tag
from kiro_crew.messaging.queue_receipt import receipt_text as _receipt_text
from kiro_crew.messaging.transport import InboundMessage


class TestInteractions:
    def _itx(self, custom_id: str, label: str = "", guild: str = "") -> DiscordInteraction:
        return DiscordInteraction(
            interaction_id="i1",
            interaction_token="tok",
            channel_id="c1",
            user_id="u1",
            message_id="m1",
            custom_id=custom_id,
            label=label,
            guild_id=guild,
        )

    @pytest.mark.asyncio
    async def test_unauthorized_interaction_not_acked(self) -> None:
        d, cli, _ = _dispatcher({"other"})
        await d.on_interaction(self._itx("a:r1:aabbccdd:1"))
        assert cli.acked == []

    @pytest.mark.asyncio
    async def test_guild_interaction_denied(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._itx("a:r1:aabbccdd:1", guild="g1"))
        assert cli.acked == []

    @pytest.mark.asyncio
    async def test_allowlisted_thread_interaction_is_acked(self) -> None:
        d, cli, _ = _dispatcher({"u1"}, allowed_threads={"c1"})
        cli.thread_channels.add("c1")
        await d.on_interaction(self._itx("a:r9:aabbccdd:1", guild="g1"))
        assert cli.acked == ["i1"]
        assert any("expired" in text for _, text, _ in cli.edits)

    @pytest.mark.asyncio
    async def test_approval_resolves_pending_future(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert fut.result() is True
            assert cli.acked == ["i1"]
            assert any("Approved" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_authorization_withdrawn_during_the_ack_stops_the_approval(self) -> None:
        """The rosters are read once before the ack, and the ack serves the REST
        ladder's own waits while the governance read after it is off-loop.

        An operator who withdraws the user across that window must not have a stale
        Approve press execute the governed tool, so the same two things the pre-ack
        gate established are read again before anything resolves.
        """
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)

        original_ack = cli.ack_component_interaction

        async def _ack_then_revoke(*args: Any, **kwargs: Any) -> None:
            await original_ack(*args, **kwargs)
            d._allowed.discard("u1")

        cli.ack_component_interaction = _ack_then_revoke  # type: ignore[method-assign]
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert cli.acked == ["i1"], "the ack itself still happens"
            assert not fut.done(), "a withdrawn user must not resolve the approval"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_the_ack_path_reads_governance_before_the_rosters(self) -> None:
        """The ceiling read is an await, so a roster reading taken before it can be
        stale by the time anything resolves. The rosters are the final word, which is
        the same contract the client's own mid-send predicate states.

        Measured from the ACK onwards: the pre-ack gate reads a roster too, and it is
        not what this pins.
        """
        d, cli, _ = _dispatcher({"u1"})
        order: list[str] = []
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)

        async def _ceiling(_channel: str) -> bool:
            order.append("ceiling")
            return True

        original_ack = cli.ack_component_interaction
        original_authorized = d._authorized

        async def _ack(*args: Any, **kwargs: Any) -> None:
            order.append("ack")
            await original_ack(*args, **kwargs)

        def _roster(user_id: str) -> bool:
            order.append("roster")
            return original_authorized(user_id)

        import kiro_crew.discord.transport_dispatch as td

        with mock.patch.object(td, "channel_inbound_permitted", _ceiling):
            cli.ack_component_interaction = _ack  # type: ignore[method-assign]
            d._authorized = _roster  # type: ignore[method-assign]
            try:
                await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            finally:
                d._authorized = original_authorized  # type: ignore[method-assign]
                DiscordApprovalDecider._REGISTRY.pop(key, None)
                DiscordApprovalDecider._NONCES.pop(key, None)
        assert "ack" in order, order
        after_ack = order[order.index("ack") + 1 :]
        assert after_ack[:2] == ["ceiling", "roster"], order

    @pytest.mark.asyncio
    async def test_a_reject_press_still_lands_after_a_withdrawal(self) -> None:
        """A REJECT is a denial, which is what a withdrawal wants. Dropping it would
        strand the pending approval until it times out.

        Its CONFIRMATION is a different thing: an outbound write into the channel.
        The reject reaches the resolution without the re-read the sibling branch does,
        and the edit may serve no wait at all, in which case the ladder's own re-check
        never runs and nothing else judges it. So the verdict is withheld.
        """
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)

        original_ack = cli.ack_component_interaction

        async def _ack_then_revoke(*args: Any, **kwargs: Any) -> None:
            await original_ack(*args, **kwargs)
            d._allowed.discard("u1")

        cli.ack_component_interaction = _ack_then_revoke  # type: ignore[method-assign]
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:0"))
            assert fut.result() is False, "the denial still resolves"
            assert cli.edits == [], "the verdict must not reach a withdrawn destination"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_a_reject_press_writes_its_verdict_while_still_authorized(self) -> None:
        """The paired case, so the guard above cannot pass by never writing at all."""
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:0"))
            assert fut.result() is False
            assert any("Denied" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @staticmethod
    def _set_channels(pdir: Any, monkeypatch, *, allow: list[str]) -> None:
        """Point the channels ceiling at a profile permitting exactly `allow`.

        Written explicitly in both directions so neither test reads whatever profile
        the host happens to carry.
        """
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir.mkdir(exist_ok=True)
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": allow}},
                }
            )
        )
        gp.reset_store()

    @pytest.mark.asyncio
    async def test_a_ceiling_denied_channel_keeps_its_card_exactly_as_posted(
        self, tmp_path, monkeypatch
    ) -> None:
        """The reverse of the verdict-lands case: with the ceiling withdrawn, the press
        writes NOTHING into the channel -- not the verdict, not a component strip.

        The card is an ordinary channel message posted while the channel was still
        permitted, so writing nothing leaves it in exactly that state. Asserted as the
        fold of everything sent and every edit applied, not merely as an empty edit
        list, because a component-only edit would also change what the channel shows.
        """
        from kiro_crew.platform import governance_profiles as gp

        d, cli, _ = _dispatcher({"u1"})
        card_id = await cli.send_message(
            "c1", "🔐 Approve `shell`?", components=[{"approve": 1, "deny": 0}]
        )
        posted = (list(cli.sent), list(cli.send_channels))
        self._set_channels(tmp_path / "profiles", monkeypatch, allow=["slack"])
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        itx = DiscordInteraction(
            interaction_id="i1",
            interaction_token="tok",
            channel_id="c1",
            user_id="u1",
            message_id=card_id,
            custom_id=f"a:r1:{nonce}:0",
            label="",
            guild_id="",
        )
        try:
            await d.on_interaction(itx)
            assert fut.result() is False, "the denial still resolves"
            assert cli.edits == [], "no verdict may be written into a denied channel"
            assert cli.component_edits == [], "the buttons must not be stripped either"
            assert (
                list(cli.sent),
                list(cli.send_channels),
            ) == posted, "the card must remain exactly the message the operator last allowed"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_the_confirmation_ceiling_is_read_after_the_ack_not_at_entry(
        self, tmp_path, monkeypatch
    ) -> None:
        """A withdrawal landing DURING the ack must still withhold the edit.

        A reading taken when the press arrived would answer `permitted` here, so this
        separates a late read from an early one; the reject arm is the only press that
        reaches the verdict without the pre-resolve gate having read the ceiling.
        """
        from kiro_crew.platform import governance_profiles as gp

        d, cli, _ = _dispatcher({"u1"})
        pdir = tmp_path / "profiles"
        self._set_channels(pdir, monkeypatch, allow=["discord"])
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        original_ack = cli.ack_component_interaction

        async def _ack_then_revoke(*args: Any, **kwargs: Any) -> None:
            await original_ack(*args, **kwargs)
            self._set_channels(pdir, monkeypatch, allow=["slack"])

        cli.ack_component_interaction = _ack_then_revoke  # type: ignore[method-assign]
        try:
            assert await td_mod.channel_inbound_permitted(
                "discord"
            ), "permitted when the press arrives"
            await d.on_interaction(self._itx(f"a:r1:{nonce}:0"))
            assert fut.result() is False, "the denial still resolves"
            assert cli.edits == [], "a withdrawal during the ack must withhold the verdict"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_the_confirmation_reads_the_ceiling_again_rather_than_reusing_it(self) -> None:
        """An APPROVE press reads the ceiling twice: once before it resolves, once
        before it writes.

        Carrying the first answer forward would be a value taken before a suspension,
        which is the defect class this change exists to close. Pinned by answering
        `permitted` to the first read and `denied` to the second, which no reuse of a
        single answer can satisfy. Both entry points share one answering function
        because the reads are on opposite sides of the resolve and read opposite
        directions: the press arriving is inbound, the verdict written is outbound.
        """
        d, cli, _ = _dispatcher({"u1"})
        answers = [True, False]
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)

        async def _ceiling(_channel: str) -> bool:
            return answers.pop(0) if answers else False

        try:
            with mock.patch.object(td_mod, "channel_inbound_permitted", _ceiling):
                with mock.patch.object(td_mod, "channel_outbound_permitted", _ceiling):
                    await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert fut.result() is True, "the approval resolved under the first reading"
            assert answers == [], "both readings must actually be taken"
            assert cli.edits == [], "the verdict must not be written after the withdrawal"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_the_confirmation_gate_reads_the_outbound_authority(self) -> None:
        """The verdict edit is a write this process makes, so the OUTBOUND ceiling
        decides it.

        Both entry points read the same `channels` allowlist, so a test that only
        watched the verdict could not tell them apart. Pinned by answering permitted
        inbound and denied OUTBOUND: the press resolves, because the inbound gate let
        it through, and the edit is still withheld, which only a gate reading the
        outbound entry point can do. Filing an egress refusal under an ingress name
        leaves an operator asking why a message did not go out reading the wrong row.
        """
        d, cli, _ = _dispatcher({"u1"})
        read: list[str] = []
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)

        async def _inbound(_channel: str) -> bool:
            read.append("inbound")
            return True

        async def _outbound(_channel: str) -> bool:
            read.append("outbound")
            return False

        try:
            with mock.patch.object(td_mod, "channel_inbound_permitted", _inbound):
                with mock.patch.object(td_mod, "channel_outbound_permitted", _outbound):
                    await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert fut.result() is True, "the inbound gate permitted the press"
            assert "outbound" in read, "the verdict gate must consult the outbound ceiling"
            assert cli.edits == [], "an outbound-denied channel gets no verdict written"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_the_confirmation_reads_the_rosters_after_the_ceiling_await(self) -> None:
        """At the verdict gate too, the rosters are the last thing read before the write.

        The ceiling read is an `await`; a roster reading taken before it describes a
        state that can have changed by the time the edit is issued. Pinned by
        withdrawing the user during the second ceiling read, which only a roster read
        placed after it can see.
        """
        d, cli, _ = _dispatcher({"u1"})
        calls: list[str] = []
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)

        async def _ceiling(_channel: str) -> bool:
            calls.append("ceiling")
            if len(calls) == 2:
                d._allowed.discard("u1")
            return True

        try:
            with mock.patch.object(td_mod, "channel_inbound_permitted", _ceiling):
                with mock.patch.object(td_mod, "channel_outbound_permitted", _ceiling):
                    await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert len(calls) == 2, calls
            assert cli.edits == [], "a roster read placed before the ceiling await is stale"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_a_dm_interaction_records_its_pairing_before_any_callback(self) -> None:
        """Every callback answers the DM channel the press arrived on, without ever
        opening it, and the mid-send re-check runs inside the first one.

        Without the pairing a rate-limited reply to an authorized presser is refused,
        which drops the reply rather than withholding it.
        """
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            assert cli.dm_pairings.get("c1") == "u1"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_a_denied_presser_records_no_pairing(self) -> None:
        """Recorded on the authorized path only, so a denied presser cannot plant a
        pairing that would answer for their channel later."""
        d, cli, _ = _dispatcher({"someone-else"})
        await d.on_interaction(self._itx("a:r1:n:1"))
        assert cli.dm_pairings == {}

    @pytest.mark.asyncio
    async def test_channels_deny_drops_approval_interaction(self, tmp_path, monkeypatch) -> None:
        # HIGH (GPT pass 1 #1 + #4): a channels-governance DENY must stop a button
        # press from resolving a pending tool approval — otherwise a policy denial
        # applied after connect could still execute a governed tool via a stale
        # approval button. This regression-locks the on_interaction chokepoint
        # (removing the gate makes the pending future resolve → test fails).
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:1"))
            # The interaction IS acked (ack happens after auth, before the gate, to
            # meet Discord's ~3s deadline — acking is a no-op UI dismissal), but the
            # approval is DROPPED before resolution: the pending future stays
            # unresolved, so the governed tool never executes.
            assert not fut.done(), "denied channel must not resolve the tool approval"
            assert cli.acked == ["i1"]
            # No verdict edit (Approved/Denied) — resolution never happened.
            assert not any("Approved" in t or "Denied" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_channels_deny_still_resolves_reject_interaction(self, tmp_path, monkeypatch):
        # A REJECT press ("a:...:0") on a denied channel
        # must STILL resolve the pending approval as refused (False) — a reject is a
        # denial, exactly what a channels-deny wants, and silently dropping it would
        # strand the pending future until timeout (~300s). Only APPROVE is gated out.
        #
        # The CONFIRMATION is separate from the resolution: it is an outbound write
        # into a channel the ceiling refuses, so it is withheld. The card
        # stays exactly as it was posted while the channel was still allowed.
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        nonce = DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx(f"a:r1:{nonce}:0"))  # reject (flag 0)
            assert fut.done() and fut.result() is False, (
                "a reject on a denied channel must resolve the approval as refused, "
                "not strand it"
            )
            assert cli.edits == [], "no verdict may be written into a denied channel"
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_channels_deny_drops_inbound_message(self, tmp_path, monkeypatch) -> None:
        # HIGH (GPT pass 1 #4): a channels DENY must stop handle_message from
        # driving a turn. Regression-locks the dispatcher's inbound chokepoint.
        import json

        from kiro_crew.platform import governance_profiles as gp

        pdir = tmp_path / "profiles"
        pdir.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
        gp.reset_store()
        (pdir / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
                }
            )
        )
        d, cli, sess = _dispatcher({"u1"})
        try:
            await d.handle_message(
                InboundMessage(
                    channel_type="discord", user_id="u1", conversation_id="c1", text="hello"
                )
            )
            # No turn ran: nothing sent, no session success recorded.
            assert cli.final_text() in (None, "")
            assert sess.successes == []
        finally:
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_wrong_nonce_reports_expiry_not_approval(self) -> None:
        """A stale button press (nonce mismatch) must not display 'Approved'."""
        d, cli, _ = _dispatcher({"u1"})
        key = DiscordApprovalDecider.key(d._session_key("u1"), "r1")
        fut: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()
        DiscordApprovalDecider._REGISTRY[key] = fut
        DiscordApprovalDecider.register_nonce(key)
        try:
            await d.on_interaction(self._itx("a:r1:0000000000000000:1"))
            assert not fut.done()
            assert any("expired" in t for _, t, _ in cli.edits)
        finally:
            DiscordApprovalDecider._REGISTRY.pop(key, None)
            DiscordApprovalDecider._NONCES.pop(key, None)

    @pytest.mark.asyncio
    async def test_expired_approval_reports_expiry(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._itx("a:r9:aabbccdd:1"))
        assert any("expired" in t for _, t, _ in cli.edits)

    @pytest.mark.asyncio
    async def test_option_choice_reinjects_as_turn(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        await d.on_interaction(self._itx(f"opt:0:{tag}", label="Choice A"))
        # Buttons retired without clobbering the answer text.
        assert cli.component_edits == [("m1", [])]
        # Choice echoed as a quote, then answered as a fresh turn.
        assert any(t.startswith("> Choice A") for t, _ in cli.sent)
        assert any(
            "Answer: Choice A" in t for t in [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        )

    @pytest.mark.asyncio
    async def test_untagged_option_press_fails_closed(self) -> None:
        """A pre-provenance button press is refused — its origin is unprovable."""
        d, cli, sess = _dispatcher({"u1"})
        await d.on_interaction(self._itx("opt:0", label="Choice A"))
        assert cli.component_edits == [("m1", [])]
        assert any("predate" in t for t, _ in cli.sent)
        assert sess.successes == []

    @pytest.mark.asyncio
    async def test_option_without_label_asks_to_type(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        tag = session_provenance_tag(d.current_session_key("u1"))
        await d.on_interaction(self._itx(f"opt:0:{tag}", label=""))
        assert any("type it instead" in t for t, _ in cli.sent)


def test_receipt_text_caps_displayed_items() -> None:
    texts = [f"message {i}" for i in range(8)]
    out = _receipt_text(texts)
    assert out.startswith("⏳ Queued (8):")
    assert "…and 3 more" in out


# ── context thresholds ───────────────────────────────────────────────────


class TestContextThresholdNotices:
    @pytest.mark.asyncio
    async def test_soft_threshold_nudges(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess.check_context_usage = lambda key, provider: 85.0  # >= soft (80)

        await d._maybe_notice("chan1", "scope1", "key", object())

        assert any("!compact" in s[0] for s in cli.sent)

    @pytest.mark.asyncio
    async def test_soft_nudge_suppressed_on_auto_managed_backend(self) -> None:
        # The nudge advises !compact, which this backend refuses — it compacts
        # on its own, so there is nothing for the user to act on.
        d, cli, sess = _dispatcher({"u1"})
        sess.check_context_usage = lambda key, provider: 85.0
        provider = SimpleNamespace(manual_compact_unsupported_backend="kas")

        await d._maybe_notice("chan1", "scope1", "key", provider)

        assert cli.sent == []

    @pytest.mark.asyncio
    async def test_below_soft_threshold_stays_silent(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess.check_context_usage = lambda key, provider: 10.0

        await d._maybe_notice("chan1", "scope1", "key", object())

        assert cli.sent == []


class TestSlashAndReplyCommands:
    """The ``!`` text surface and the registered ``/`` surface share handlers.

    The point of these tests is that a command cannot exist on one surface and
    not the other, and that the slash surface answers its own interaction
    ephemerally rather than posting to the channel.
    """

    def _cmd(
        self, name: str, options: dict[str, str] | None = None, guild: str = ""
    ) -> DiscordInteraction:
        return DiscordInteraction(
            interaction_id="i9",
            interaction_token="tok",
            channel_id="c1",
            user_id="u1",
            message_id="",
            guild_id=guild,
            kind=2,
            command_name=name,
            options=options or {},
        )

    @pytest.mark.asyncio
    async def test_status_reports_over_the_text_surface(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(_inbound("!status"))
        assert cli.sent, "status must reply"
        body = cli.sent[-1][0]
        assert "uptime" in body and "YOLO" in body

    @pytest.mark.asyncio
    async def test_status_over_slash_answers_the_interaction_ephemerally(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._cmd("status"))
        # The reply rides the interaction callback, NOT a channel message: an
        # ephemeral answer is the whole reason the slash surface exists here.
        assert cli.sent == []
        assert len(cli.responses) == 1
        interaction_id, body, ephemeral = cli.responses[0]
        assert interaction_id == "i9" and ephemeral is True and "uptime" in body

    @pytest.mark.asyncio
    async def test_a_slash_command_is_never_pre_acked_as_a_component(self) -> None:
        """DEFERRED_UPDATE_MESSAGE is component-only, and the first response is
        the command's only route — spending it on an ack would strand the reply."""
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._cmd("status"))
        assert cli.acked == []

    @pytest.mark.asyncio
    async def test_help_comes_from_the_shared_catalogue_on_both_surfaces(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(_inbound("!help"))
        text_card = cli.sent[-1][0]
        await d.on_interaction(self._cmd("help"))
        slash_card = cli.responses[-1][1]
        assert text_card == slash_card
        # Every catalogued command appears, so the card cannot drift from the
        # registered menu.
        for name, _desc in COMMAND_SPEC:
            assert name in text_card

    @pytest.mark.asyncio
    async def test_an_unknown_slash_command_does_not_reach_the_model(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.on_interaction(self._cmd("nope"))
        # It replays through the text path, where an unrecognized `!nope` is
        # ordinary text; what must NOT happen is a silent drop with no reply.
        assert cli.responses, "the interaction must be answered either way"


class TestModelPicker:
    @pytest.mark.asyncio
    async def test_no_advertised_models_says_so_instead_of_posting_empty_buttons(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(_inbound("!model"))
        assert "No model list available yet" in cli.sent[-1][0]
        assert cli.sent[-1][1] is None

    @pytest.mark.asyncio
    async def test_picker_posts_buttons_and_a_press_applies_the_choice(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.handle_message(_inbound("!model"))
        _text, components = cli.sent[-1]
        ids = [b["custom_id"] for row in components for b in row["components"]]
        # Index-keyed, never the model id: a custom_id is capped at 100 chars and
        # Discord replays old ones indefinitely.
        assert ids == ["m:0", "m:1"]

        message_id = "101"
        await d.on_interaction(
            DiscordInteraction(
                interaction_id="i1",
                interaction_token="tok",
                channel_id="c1",
                user_id="u1",
                message_id=message_id,
                custom_id="m:1",
            )
        )
        assert d._model_pref[d._scope_id("u1", "")] == "m-fast"
        # One edit carries the outcome AND retires the buttons.
        last_id, body, components = cli.edits[-1]
        assert last_id == message_id and components == [] and "m-fast" in body
        # The live session was switched in place, not merely recorded.
        assert sess._gp.set_models == ["m-fast"]

    @pytest.mark.asyncio
    async def test_a_second_press_is_refused_rather_than_applied_twice(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.handle_message(_inbound("!model"))
        press = DiscordInteraction(
            interaction_id="i1",
            interaction_token="tok",
            channel_id="c1",
            user_id="u1",
            message_id="101",
            custom_id="m:1",
        )
        await d.on_interaction(press)
        await d.on_interaction(press)
        assert "no longer active" in cli.edits[-1][1]

    @pytest.mark.asyncio
    async def test_an_expired_picker_is_refused_and_names_no_model(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.handle_message(_inbound("!model"))
        for picker in d._model_pickers.values():
            picker.created_at -= td_mod._MODEL_PICKER_TTL_SECS + 1
        await d.on_interaction(
            DiscordInteraction(
                interaction_id="i1",
                interaction_token="tok",
                channel_id="c1",
                user_id="u1",
                message_id="101",
                custom_id="m:1",
            )
        )
        assert "no longer active" in cli.edits[-1][1]
        assert d._model_pref == {}

    @pytest.mark.asyncio
    async def test_an_out_of_range_or_unparseable_index_is_refused(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        for bad in ("m:99", "m:notanint", "m:-1"):
            await d.handle_message(_inbound("!model"))
            await d.on_interaction(
                DiscordInteraction(
                    interaction_id="i1",
                    interaction_token="tok",
                    channel_id="c1",
                    user_id="u1",
                    message_id=cli.sent[-1] and str(100 + len(cli.sent)),
                    custom_id=bad,
                )
            )
            assert d._model_pref == {}, bad

    @pytest.mark.asyncio
    async def test_the_picked_model_reaches_the_next_cold_start(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.handle_message(_inbound("!model"))
        await d.on_interaction(
            DiscordInteraction(
                interaction_id="i1",
                interaction_token="tok",
                channel_id="c1",
                user_id="u1",
                message_id="101",
                custom_id="m:1",
            )
        )
        await d.handle_message(_inbound("hello"))
        assert sess.last_model == "m-fast"

    @pytest.mark.asyncio
    async def test_pickers_are_pruned_so_a_press_less_model_cannot_grow_forever(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        for i in range(td_mod._MODEL_PICKER_MAX + 10):
            d._model_pickers[f"c1:{i}"] = td_mod._ModelPicker(
                scope_id="s",
                channel_id="c1",
                message_id=str(i),
                # flake-ok: live-clock stamp; asserts the count cap, the 300 s TTL cannot expire
                created_at=time.time(),
                choices=(("", "Auto"),),
            )
        # flake-ok: live-clock read; asserts the count cap, the 300 s TTL cannot expire
        d._prune_model_pickers(time.time())
        assert len(d._model_pickers) == td_mod._MODEL_PICKER_MAX


class TestCommandSurfaceParity:
    """Every catalogued command must actually do something on BOTH surfaces.

    This is the drift guard the two-surface design needs: adding a row to
    ``COMMAND_SPEC`` publishes it to Discord's menu, so a row with no handler
    ships a visible command that silently does nothing. Walking the catalogue
    rather than a hand-written list is the point, since the hand-written list is
    what goes stale.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [name for name, _desc in COMMAND_SPEC])
    async def test_every_catalogued_command_is_answered_over_slash(self, name: str) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        with (
            mock.patch("kiro_crew.dashboard.token_auth.generate_token", return_value="TKN"),
            mock.patch.object(td_mod, "safety_override") as so,
            mock.patch.object(td_mod, "describe_grant_lifetime", return_value="30m"),
        ):
            so.return_value.is_active.return_value = False
            await d.on_interaction(
                DiscordInteraction(
                    interaction_id="i1",
                    interaction_token="tok",
                    channel_id="c1",
                    user_id="u1",
                    message_id="",
                    kind=2,
                    command_name=name,
                )
            )
        # Either the interaction itself was answered, or it was acknowledged and
        # replayed onto the text path which then replied. Silence is the failure.
        assert cli.responses or cli.sent, name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", [name for name, _desc in COMMAND_SPEC])
    async def test_every_catalogued_command_resolves_on_both_prefixes(self, name: str) -> None:
        assert parse_command(f"!{name}") == name
        assert parse_command(f"/{name}") == name

    def test_the_registered_payload_covers_exactly_the_catalogue(self) -> None:
        assert [row["name"] for row in application_command_payload()] == [
            name for name, _desc in COMMAND_SPEC
        ]


class TestTheProcessGrantAnswersDiscordPermissions:
    """Discord's turns read the operator's process-wide YOLO grant on every
    permission request, before the approval buttons -- the pipeline's default
    ``Approvals.grant``, which Discord does not spell out. This is the one test of
    the REAL dispatcher consulting it: a Discord site that dropped the grant would
    leave YOLO silently inert there."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("granted", [True, False])
    async def test_an_interactive_permission_is_answered_by_the_grant_first(
        self, monkeypatch, granted: bool
    ) -> None:
        from kiro_crew.safety_override import safety_override

        answered: list[tuple[str, str]] = []
        asked: list[str] = []

        async def _stream(self: Any, message: str) -> Any:
            yield AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="r1",
                title="Run a tool",
                options=[{"optionId": "allow", "name": "Allow"}],
            )
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        async def _approve(self: Any, request_id: Any) -> None:
            answered.append(("approve", request_id))

        async def _reject(self: Any, request_id: Any) -> None:
            answered.append(("reject", request_id))

        async def _buttons(self: Any, event: Any) -> bool:
            asked.append(event.request_id)
            return False  # the person denies, so a missing grant reads as a rejection

        monkeypatch.setattr(FakeProvider, "stream", _stream)
        monkeypatch.setattr(FakeProvider, "approve_tool", _approve)
        monkeypatch.setattr(FakeProvider, "reject_tool", _reject)
        monkeypatch.setattr(DiscordApprovalDecider, "__call__", _buttons)
        if granted:
            assert safety_override().activate("dashboard").active
        d, _cli, _sess = _dispatcher({"u1"})
        d.approval_mode = "interactive"

        await d.handle_message(_inbound("run it"))

        if granted:
            assert answered == [("approve", "r1")] and asked == [], "the grant answers first"
        else:
            assert answered == [("reject", "r1")] and asked == ["r1"], "the buttons decide"


class TestRenderTogglesAreWiredPerTurn:
    """The dispatcher must actually FEED the render toggles to the renderer.

    A constructor argument with a default is inert until something passes it, and
    both defaults happen to be the shipped values, so a missing wire looks exactly
    like a working feature until an operator changes the setting.
    """

    @pytest.mark.asyncio
    async def test_the_dispatcher_reads_the_toggles_fresh_for_each_turn(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        seen: list[tuple[bool, bool]] = []
        real = td_mod.DiscordRenderer

        def _spy(*args: Any, **kwargs: Any) -> Any:
            seen.append((kwargs["reactions_enabled"], kwargs["show_thinking"]))
            return real(*args, **kwargs)

        # The dispatcher also reads `DiscordRenderer.channel_type` as a CLASS
        # attribute for the mute check, so the stand-in has to carry it.
        _spy.channel_type = real.channel_type  # type: ignore[attr-defined]

        with (
            mock.patch.object(td_mod, "DiscordRenderer", _spy),
            _live_discord(reactions_enabled=False, show_thinking=True),
        ):
            await d.handle_message(_inbound("hi"))
        # Read per TURN, not off the boot config, so the dashboard toggle takes
        # effect on the next message instead of the next restart.
        assert seen == [(False, True)]

    def test_an_unreadable_config_keeps_the_shipped_defaults(self) -> None:
        """Neither toggle is a security control, so a failed load must not fail
        the turn: reactions stay on (the loud default) and reasoning stays off
        (the quiet one)."""
        d, _cli, _sess = _dispatcher({"u1"})
        with mock.patch("kiro_crew.config.loader.KiroCrewConfig.load", side_effect=OSError):
            assert d._render_config() == (True, False)


class TestGuildCommandRefusalsAreVisible:
    """A refused command must SAY so; Discord's own error is not a message.

    An interaction the bot never answers renders as a red "The application did
    not respond" with no reason, which reads as the bot being broken rather than
    as a rule. Every refusal on the command path therefore answers ephemerally,
    which discloses nothing to the rest of a shared channel.
    """

    def _cmd(self, *, guild: str = "", channel: str = "c1") -> DiscordInteraction:
        return DiscordInteraction(
            interaction_id="i1",
            interaction_token="tok",
            channel_id=channel,
            user_id="u1",
            message_id="",
            guild_id=guild,
            kind=2,
            command_name="status",
        )

    @pytest.mark.asyncio
    async def test_a_command_in_an_unapproved_guild_channel_is_told_why(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        await d.on_interaction(self._cmd(guild="g1", channel="shared"))
        assert len(cli.responses) == 1
        _iid, body, ephemeral = cli.responses[0]
        assert ephemeral is True
        assert "approved thread" in body and "DM" in body

    @pytest.mark.asyncio
    async def test_a_governance_denied_command_is_told_why_without_naming_policy(self) -> None:
        d, cli, _ = _dispatcher({"u1"})
        with mock.patch.object(td_mod, "channel_inbound_permitted", return_value=False):
            await d.on_interaction(self._cmd())
        assert len(cli.responses) == 1
        _iid, body, ephemeral = cli.responses[0]
        assert ephemeral is True and "disabled by policy" in body
        # The profile's CONTENTS are the operator's ceiling, not the user's to read.
        assert "profile" not in body.lower() and "scope" not in body.lower()

    @pytest.mark.asyncio
    async def test_an_unauthorized_user_gets_nothing_at_all(self) -> None:
        """Deny-by-default stays SILENT for an unknown user: an unauthorized
        sender must learn nothing about what they reached."""
        d, cli, _ = _dispatcher({"someone-else"})
        await d.on_interaction(self._cmd())
        assert cli.responses == [] and cli.sent == []

    @pytest.mark.asyncio
    async def test_an_approved_thread_still_answers_the_command(self) -> None:
        d, cli, _ = _dispatcher({"u1"}, allowed_threads={"t1"})
        cli.thread_channels.add("t1")
        await d.on_interaction(self._cmd(guild="g1", channel="t1"))
        assert len(cli.responses) == 1
        assert "uptime" in cli.responses[0][1]


class TestRendererIsFullyWired:
    """The renderer's optional arguments are inert until something passes them.

    Both of these defaults are the shipped value, so a missing wire looks exactly
    like a working feature from the outside: the ladder simply never arms and the
    footer simply has no context chip. Only a test that observes the ARGUMENT
    catches it.
    """

    @pytest.mark.asyncio
    async def test_the_inbound_message_id_arms_the_phase_ladder(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        seen: list[str] = []
        real = td_mod.DiscordRenderer

        def _spy(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["react_message_id"])
            return real(*args, **kwargs)

        _spy.channel_type = real.channel_type  # type: ignore[attr-defined]
        with mock.patch.object(td_mod, "DiscordRenderer", _spy):
            await d.handle_message(_inbound_with_id("hi", message_id="m42"))
        # The phase emoji goes on the user's OWN message, so the ladder cannot
        # arm without its id.
        assert seen == ["m42"]

    @pytest.mark.asyncio
    async def test_a_turn_with_no_inbound_message_arms_nothing(self) -> None:
        """A synthetic turn (an option press, an AutoNudge fire) has no message
        to react to, so the ladder must stay down rather than react to nothing."""
        d, _cli, _sess = _dispatcher({"u1"})
        seen: list[str] = []
        real = td_mod.DiscordRenderer

        def _spy(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["react_message_id"])
            return real(*args, **kwargs)

        _spy.channel_type = real.channel_type  # type: ignore[attr-defined]
        with mock.patch.object(td_mod, "DiscordRenderer", _spy):
            await d.handle_message(_inbound("hi"))
        assert seen == [""]

    @pytest.mark.asyncio
    async def test_the_session_provider_is_bound_as_the_context_source(self) -> None:
        d, _cli, sess = _dispatcher({"u1"})
        bound: list[Any] = []
        real = td_mod.DiscordRenderer

        def _spy(*args: Any, **kwargs: Any) -> Any:
            renderer = real(*args, **kwargs)
            original = renderer.bind_context_source

            def _record(provider: Any) -> None:
                bound.append(provider)
                original(provider)

            renderer.bind_context_source = _record  # type: ignore[method-assign]
            return renderer

        _spy.channel_type = real.channel_type  # type: ignore[attr-defined]
        with mock.patch.object(td_mod, "DiscordRenderer", _spy):
            await d.handle_message(_inbound("hi"))
        # Unbound, the turn footer's context chip cannot render at all.
        assert bound == [sess.last_provider]


class TestOptionChoiceIsNeverACommand:
    @pytest.mark.asyncio
    async def test_a_model_authored_option_label_cannot_execute_a_command(self) -> None:
        """An option label is chosen by the MODEL; the press only says which one
        the user picked. Interpreting it would put a destructive command one tap
        away -- `!new` discards the conversation the user was mid-way through."""
        d, cli, sess = _dispatcher({"u1"})
        before = d._session_key("u1")
        await d.on_interaction(
            DiscordInteraction(
                interaction_id="i1",
                interaction_token="tok",
                channel_id="c1",
                user_id="u1",
                message_id="m1",
                custom_id=f"opt:0:{session_provenance_tag(before)}",
                label="!new",
            )
        )
        # `!new` rotates the generation suffix, so an unchanged session key is
        # positive proof the label never executed.
        assert d._session_key("u1") == before
        # And the turn DID run: a command returns before the session is acquired,
        # so a provider having been reached is what proves the label was treated
        # as chat text. Asserting on the posted text alone cannot distinguish
        # them, because this path echoes the chosen label either way.
        assert sess.last_provider is not None
        assert any("Answer" in text for text, _c in cli.sent) or any(
            "Answer" in text for _mid, text, _c in cli.edits
        )


class TestReviewFindingRegressions:
    """Guards for the four findings the PR review raised."""

    @pytest.mark.asyncio
    async def test_an_ambiguous_allowlist_gets_no_owner_dm(self) -> None:
        """With no owner field and several allow-listed users, picking the first
        would send private agent output to the wrong human."""
        from kiro_crew.dashboard.handlers.messaging import _owner_dm_target

        def _t(tid: str, available: bool = True) -> Any:
            return SimpleNamespace(target_id=tid, available=available)

        one = SimpleNamespace(configured_targets=lambda: [_t("user:1")])
        many = SimpleNamespace(configured_targets=lambda: [_t("user:1"), _t("user:2")])
        assert _owner_dm_target(one) == "user:1"
        assert _owner_dm_target(many) == ""
        # A thread is a wider audience than a DM and never counts as one.
        threads = SimpleNamespace(configured_targets=lambda: [_t("thread:9"), _t("user:1")])
        assert _owner_dm_target(threads) == "user:1"

    @pytest.mark.asyncio
    async def test_guild_slash_model_refuses_rather_than_posting_the_list(self) -> None:
        """The slash surface promises a private reply, and the picker cannot be
        one: its buttons need an editable channel message."""
        d, cli, sess = _dispatcher({"u1"}, allowed_threads={"t1"})
        cli.thread_channels.add("t1")
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.on_interaction(
            DiscordInteraction(
                interaction_id="i1",
                interaction_token="tok",
                channel_id="t1",
                user_id="u1",
                message_id="",
                guild_id="g1",
                kind=2,
                command_name="model",
            )
        )
        assert len(cli.responses) == 1 and cli.responses[0][2] is True
        assert "cannot be private here" in cli.responses[0][1]
        # Nothing was posted to the thread, so no model list leaked.
        assert cli.sent == []

    @pytest.mark.asyncio
    async def test_a_dm_slash_model_still_posts_the_picker(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._gp.models = [{"modelId": "m-fast", "name": "Fast"}]
        await d.on_interaction(
            DiscordInteraction(
                interaction_id="i1",
                interaction_token="tok",
                channel_id="c1",
                user_id="u1",
                message_id="",
                kind=2,
                command_name="model",
            )
        )
        assert any(components for _text, components in cli.sent)

    def test_the_install_url_grants_thread_creation(self) -> None:
        """`auto_thread` promotes a channel message into a NEW public thread, so
        an install without this bit answers nothing in an allowed channel."""
        from kiro_crew.discord.install_url import (
            PERM_CREATE_PUBLIC_THREADS,
            THREAD_PERMISSIONS,
        )

        assert THREAD_PERMISSIONS & PERM_CREATE_PUBLIC_THREADS


class TestPerTurnConfigReadIsOffLoop:
    @pytest.mark.asyncio
    async def test_the_render_config_read_is_offloaded(self) -> None:
        """The per-turn read is a real config.json read plus schema validation, so
        on the gateway's single loop it stalls every other chat and heartbeat task.
        Reading fresh is the point, so it can only be moved, not cached away."""
        d, _cli, _sess = _dispatcher({"u1"})
        offloaded: list[Any] = []
        real = asyncio.to_thread

        async def _spy(fn: Any, *a: Any, **kw: Any) -> Any:
            offloaded.append(getattr(fn, "__name__", ""))
            return await real(fn, *a, **kw)

        with mock.patch.object(td_mod.asyncio, "to_thread", _spy):
            await d.handle_message(_inbound("hi"))
        assert "_render_config" in offloaded


# ── !agent (show and switch the conversation's agent) ──


def _agent_roster():
    from kiro_crew.agent_discovery import AgentInfo

    return [
        AgentInfo(name="beta", filename="beta.json", description="", model=""),
        AgentInfo(
            name="kirocrew-worker",
            filename="kirocrew-worker.json",
            description="",
            model="",
            kirocrew_owned=True,
        ),
    ]


class TestAgentCommand:
    @staticmethod
    def _msg(text: str) -> InboundMessage:
        return InboundMessage(channel_type="discord", user_id="u1", conversation_id="c1", text=text)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["!agent beta", "/agent beta"])
    async def test_agent_name_switches_the_next_turn(self, monkeypatch, command) -> None:
        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster)
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg(command))
        assert any("Agent set to `beta`" in t for t, _ in cli.sent)
        await d.handle_message(self._msg("hi"))
        assert sess.last_agent == "beta"

    @pytest.mark.asyncio
    async def test_status_names_the_picked_agent(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster)
        d, cli, _ = _dispatcher({"u1"})
        await d.handle_message(self._msg("!agent beta"))
        await d.handle_message(self._msg("!status"))
        assert "agent `beta`" in cli.sent[-1][0]

    @pytest.mark.asyncio
    async def test_internal_agent_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster)
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(self._msg("!agent kirocrew-worker"))
        assert any("No agent named" in t for t, _ in cli.sent)
        await d.handle_message(self._msg("hi"))
        assert sess.last_agent == "kirocrew"
