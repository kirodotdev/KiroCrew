"""Recipient authorization on the PROACTIVE send path, per transport.

``authorize`` gates a turn a user drove. Nothing gated the messages nobody asked
for -- a cron result, a compaction notice, a subagent completion -- because those
resolve their destination from a **persisted** ``ChannelLink``, and a link records
a conversation but not the principal that authorized it. Remove a recipient from a
channel's allow-list, restart, and every proactive leg still resolved that link and
still sent to them: the roster had changed and nothing re-read it.

Two halves, mirroring ``test_messaging_import_purity``:

* an **enumerate-once gate** requiring every shipped transport to override
  ``may_send_to`` and make its own decision explicit, so a channel cannot inherit
  the permissive ABC default silently, and a NEW channel cannot skip the question;
* **behavioral** tests per transport that can answer authoritatively, plus the
  shared chokepoint that consults them.

The gate is AST-based on purpose: it needs to see every channel package without
importing eight clients' optional dependencies, and a transport that fails to
import is exactly the one whose gap would go unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import kiro_crew as kiro_crew_pkg
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import MessagingTransport

#: The method every shipped transport must decide for itself.
_HOOK = "may_send_to"

#: Transports whose conversation id is opaque, so they answer via the session's
#: *principal* instead and permit only when the key names no single person (a
#: room-audience route or a unified bucket). Recorded so the residue stays visible:
#: removing a row means that channel became answerable without a principal too.
_PRINCIPAL_ANSWERED = {
    "discord": "DM channel id is not the user snowflake; create_dm_channel is a POST",
    "webex": "a DM session binds room_id while the email roster holds emails "
    "(a space answers from its own room allow-list instead)",
}

#: No shipped transport currently permits every persisted destination without a
#: channel-specific revocation decision. Kept as a typed table so a future
#: unavoidable exception must still record its reason at the method.
_PERMITS_WITH_REASON: dict[str, str] = {}


def _transport_classes() -> dict[str, list[str]]:
    """``channel -> [MessagingTransport subclass names]`` across the package."""
    root = Path(kiro_crew_pkg.__file__).parent
    found: dict[str, list[str]] = {}
    for path in sorted(root.glob("*/transport.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            # Bare name or dotted; either way the LAST component is the class.
            bases = [
                base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                for base in node.bases
            ]
            if "MessagingTransport" in bases:
                found.setdefault(path.parent.name, []).append(node.name)
    return found


def _overrides_hook(channel: str, class_name: str) -> bool:
    """Whether *class_name* defines :data:`_HOOK` in its OWN body."""
    path = Path(kiro_crew_pkg.__file__).parent / channel / "transport.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == _HOOK
                for item in node.body
            )
    return False


class TestEveryTransportDecidesForItself:
    def test_the_scan_finds_every_channel(self) -> None:
        """A glob that matched nothing would make the whole gate vacuous."""
        found = _transport_classes()
        # The shipped channels. Named explicitly rather than counted: a
        # count passes just as happily when a channel is renamed out of the scan.
        for channel in (
            "discord",
            "imessage",
            "slack",
            "teams",
            "telegram",
            "webex",
            "wecom",
            "weixin",
            "whatsapp",
        ):
            assert channel in found, f"{channel} has no MessagingTransport subclass in the scan"

    def test_every_transport_overrides_may_send_to(self) -> None:
        missing = [
            f"{channel}.{name}"
            for channel, names in _transport_classes().items()
            for name in names
            if not _overrides_hook(channel, name)
        ]
        assert not missing, (
            f"these transports inherit the permissive MessagingTransport.{_HOOK} default: "
            f"{missing}. A proactive send resolves a PERSISTED link, so each transport must "
            "decide whether that conversation's principal is still on its roster -- or "
            "override with the reason it cannot, so the gap is greppable."
        )

    def test_recorded_permits_still_name_real_channels(self) -> None:
        """A stale row would silently excuse a channel that does not exist."""
        found = _transport_classes()
        stale = [
            channel
            for channel in (*_PERMITS_WITH_REASON, *_PRINCIPAL_ANSWERED)
            if channel not in found
        ]
        assert not stale, f"remove these rows: {stale}"

    def test_the_abc_default_is_permissive_and_that_is_why_the_gate_exists(self) -> None:
        """Pins the default the gate above compensates for.

        Documents the trade rather than asserting a preference: a fail-closed ABC
        default would deny every channel that has not implemented the hook,
        including any out-of-tree transport, so the default permits and CI is what
        makes the shipped channels decide.
        """

        class _Bare(MessagingTransport):
            channel_type = "bare"

            async def send_message(
                self, conversation_id: str, content: str, thread_id: str | None = None
            ) -> str:
                return ""

            async def resolve_conversation(self, user_id: str) -> str:
                return user_id

            async def fetch_history(
                self, conversation_id: str, thread_id: str | None = None
            ) -> list[Any]:
                return []

            async def receive(self, raw_envelope: Any) -> None:
                return None

            def authorize(self, msg: Any) -> bool:
                return False

        assert _Bare().may_send_to("anything") is True


class TestTelegramOutboundAuthz:
    """The channel the gap was reported against, and the one that can answer."""

    def _transport(self, **over: Any) -> Any:
        from kiro_crew.telegram.transport import TelegramTransport

        kwargs: dict[str, Any] = {
            "allowed_user_ids": [111],
            "allow_forum": False,
            "allowed_forum_chat_ids": (),
        }
        kwargs.update(over)
        return TelegramTransport(object(), **kwargs)

    def test_an_allow_listed_dm_is_permitted(self) -> None:
        # chat_id == user_id in a Telegram private chat, which is exactly why
        # this transport can re-decide the question at all.
        assert self._transport().may_send_to("111") is True

    def test_a_revoked_dm_is_refused(self) -> None:
        """The reported scenario: recipient dropped from the roster, link intact."""
        assert self._transport(allowed_user_ids=[222]).may_send_to("111") is False

    def test_an_empty_roster_authorizes_nobody(self) -> None:
        assert self._transport(allowed_user_ids=[]).may_send_to("111") is False

    def test_an_empty_conversation_is_refused(self) -> None:
        assert self._transport().may_send_to("") is False

    def test_an_allow_listed_forum_topic_is_permitted(self) -> None:
        t = self._transport(allow_forum=True, allowed_forum_chat_ids=[-100123])
        assert t.may_send_to("-100123", "7") is True

    def test_a_forum_topic_off_the_chat_allow_list_is_refused(self) -> None:
        t = self._transport(allow_forum=True, allowed_forum_chat_ids=[-100999])
        assert t.may_send_to("-100123", "7") is False

    def test_a_forum_topic_is_refused_when_forums_are_disabled(self) -> None:
        """Turning the feature off must stop its proactive traffic too."""
        t = self._transport(allow_forum=False, allowed_forum_chat_ids=[-100123])
        assert t.may_send_to("-100123", "7") is False

    def test_a_forum_link_is_not_waved_through_by_the_dm_roster(self) -> None:
        """A thread_id must route to the forum gate, not the user allow-list.

        Without the thread_id branch a supergroup chat_id would be tested against
        ``allowed_user_ids`` -- refusing legitimate Topics, and permitting any
        supergroup whose id happened to be allow-listed as a user.
        """
        t = self._transport(allowed_user_ids=[-100123], allow_forum=False)
        assert t.may_send_to("-100123", "7") is False

    @pytest.mark.parametrize("bad", ["not-a-number", "12x", ""])
    def test_a_malformed_forum_chat_id_is_refused(self, bad: str) -> None:
        t = self._transport(allow_forum=True, allowed_forum_chat_ids=[-100123])
        assert t.may_send_to(bad, "7") is False


class TestOtherTransportsThatCanAnswer:
    def test_imessage_matches_the_handle_roster(self) -> None:
        from kiro_crew.imessage.transport import IMessageTransport

        t = IMessageTransport(object(), allowed_handles=["+15550100"])
        assert t.may_send_to("+15550100") is True
        assert t.may_send_to("+15550199") is False

    def test_imessage_normalizes_the_conversation_id_it_is_given(self) -> None:
        """A link stored in one spelling must match the normalized roster.

        The roster is normalized at construction, so the spelling difference has
        to be on the INPUT side to test anything: a roster written the pretty way
        and a link written the plain way both normalize, and asserting that
        direction passes with the normalization deleted.
        """
        from kiro_crew.imessage.transport import IMessageTransport

        t = IMessageTransport(object(), allowed_handles=["+15550100"])
        assert t._allowed == frozenset({"+15550100"})
        # The link carries formatting the roster entry does not.
        assert t.may_send_to("+1 (555) 010-0") is True
        assert t.may_send_to("+1 (555) 019-9") is False

    @staticmethod
    def _teams(allowed: list[str], *, owner: str = "", conversation: str = "") -> Any:
        """A Teams transport with *conversation* recorded as *owner*'s.

        Seeded through the real ``ServiceUrlStore``, which is where the identity
        mapping lives, so the test exercises the same lookup the send path does
        rather than a dict this assertion invented.
        """
        from kiro_crew.teams.transport import TeamsTransport

        t = TeamsTransport(object(), allowed_emails=allowed)
        if owner and conversation:
            t._store.remember(conversation, "https://smba.trafficmanager.net/", identity=owner)
            # `remember` does not flip the load flag, but a store holding rows has
            # been read. Without this the transport sits in its unread window,
            # where it permits by design, and every assertion below would pass for
            # the wrong reason.
            t._store._loaded = True
        return t

    def test_teams_reverse_maps_the_conversation_then_checks_the_roster(self) -> None:
        t = self._teams(["alice@example.com"], owner="alice@example.com", conversation="conv-1")
        assert t.may_send_to("conv-1") is True
        # An unknown conversation is refused even with a populated store.
        assert t.may_send_to("conv-2") is False

    def test_teams_refuses_while_its_persisted_route_store_is_unread(self) -> None:
        """An unread store is a refusal, because the gateway makes it unreachable.

        The two answers here are both wrong on their own: permitting leaves a startup
        window where a recipient already off the allow-list is still reachable, since
        ``send_message`` awaits its own ``ensure_loaded`` and would reload the
        persisted route and deliver. Refusing would drop a send the transport can
        complete. The gateway removes the choice by awaiting ``warm_routes()`` BEFORE
        registering the transport, so nothing reaches this check with the store
        unread and the fail-closed answer costs nothing.
        """
        t = self._teams(["alice@example.com"])
        assert t._store.loaded is False
        assert t.may_send_to("conv-1") is False

    @pytest.mark.asyncio
    async def test_teams_warm_routes_precedes_registration_in_the_gateway(self) -> None:
        """The ordering the refusal above depends on, asserted rather than assumed.

        Without it the fail-closed answer silently becomes a startup outage for every
        Teams proactive send, which no unit test of the predicate alone would show.
        """
        import inspect

        from kiro_crew.teams import gateway as teams_gateway

        src = inspect.getsource(teams_gateway)
        warm = src.index("await transport.warm_routes()")
        register = src.index("state.register_channel_transport(transport)")
        assert warm < register, (
            "warm_routes() must be awaited before register_channel_transport(), or "
            "may_send_to refuses every send until the store happens to load"
        )

    def test_teams_enforces_once_the_store_has_been_read(self) -> None:
        """Non-vacuity for the window above: the permit is scoped to it."""
        t = self._teams(["alice@example.com"], owner="alice@example.com", conversation="conv-1")
        # `remember` does not flip the load flag; mark it read as `ensure_loaded` does.
        t._store._loaded = True
        assert t.may_send_to("conv-1") is True
        assert t.may_send_to("conv-2") is False

    def test_teams_refuses_a_conversation_whose_owner_was_revoked(self) -> None:
        # Learned while Alice was allowed; she is not on the roster.
        t = self._teams(["bob@example.com"], owner="alice@example.com", conversation="conv-1")
        assert t.may_send_to("conv-1") is False

    def _weixin(self, policy: str, allowed: list[str]) -> Any:
        from kiro_crew.weixin.transport import WeixinTransport

        return WeixinTransport(
            object(),
            account_id="acct",
            ctx_store=object(),
            allowed_user_ids=allowed,
            dm_policy=policy,
        )

    def test_weixin_honors_each_dm_policy(self) -> None:
        assert self._weixin("open", []).may_send_to("u1") is True
        assert self._weixin("allowlist", ["u1"]).may_send_to("u1") is True
        assert self._weixin("allowlist", ["u2"]).may_send_to("u1") is False
        assert self._weixin("disabled", ["u1"]).may_send_to("u1") is False

    def test_weixin_denies_an_unrecognized_policy(self) -> None:
        """Fail closed on a typo, matching ``authorize``."""
        assert self._weixin("allowlst", ["u1"]).may_send_to("u1") is False

    def test_weixin_ignores_the_learned_known_users_set(self) -> None:
        """Learned identities must not survive removal from the roster.

        ``resolve_configured_target`` accepts ``_known_users``; this must not, or
        a peer who spoke once keeps receiving proactive messages forever.
        """
        t = self._weixin("allowlist", [])
        t._known_users.add("u1")
        assert t.may_send_to("u1") is False

    def test_wecom_mirrors_authorize(self) -> None:
        from kiro_crew.wecom.transport import WeComTransport

        t = WeComTransport(object(), allowed_users=["u1"], owner_id="owner")
        assert t.may_send_to("u1") is True
        assert t.may_send_to("owner") is True
        # A one-shot response_url is not a roster identity.
        assert t.may_send_to("https://example.invalid/reply/abc") is False


class TestPrincipalAnsweredTransports:
    """Discord and Webex: opaque conversation id, so the principal decides.

    These are the two whose roster could not be reached from the link at all. The
    session key supplies the principal for a 1:1 DM, which is the case a revocation
    actually concerns, and the room-audience routes stay permitted because the
    roster holds users rather than rooms.
    """

    def _discord(self, allowed: list[str], threads: list[str] | None = None) -> Any:
        from kiro_crew.discord.transport import DiscordTransport

        # The transport installs its mid-send authorization predicate onto the
        # client it is handed, so this stand-in has to accept an attribute --
        # a bare ``object()`` cannot. Nothing else about the client is reached
        # here: ``may_send_to`` decides from the rosters alone.
        class _UnusedClient:
            pass

        return DiscordTransport(
            _UnusedClient(), allowed_user_ids=allowed, allowed_thread_ids=threads or []
        )

    def test_discord_permits_an_allow_listed_principal(self) -> None:
        assert self._discord(["42"]).may_send_to("dm-chan-1", principal="42") is True

    def test_discord_refuses_a_revoked_principal(self) -> None:
        """The reported scenario, now decided rather than waved through.

        The conversation id is an opaque DM channel id that IS still the right
        destination; only the principal reveals that its owner was removed.
        """
        assert self._discord(["99"]).may_send_to("dm-chan-1", principal="42") is False

    def test_discord_refuses_a_principal_against_an_empty_roster(self) -> None:
        assert self._discord([]).may_send_to("dm-chan-1", principal="42") is False

    def test_discord_permits_an_allow_listed_thread_with_no_thread_id(self) -> None:
        """The shape Discord actually persists: channel_id only, no thread_id.

        ``_bind_origin_mirror`` builds ``ChannelLink("discord", channel_id=...)``, so
        a check keyed on ``thread_id`` never fires for a thread and every thread
        session falls to the DM arm, where a forum key names no principal and the
        send is refused. That is a silent drop of cron results and subagent
        completions into an allow-listed thread, so the match is on the conversation
        id, which for a Discord thread IS its snowflake.
        """
        t = self._discord(["42"], threads=["thread-1"])
        assert t.may_send_to("thread-1") is True
        # And still permitted when a thread_id happens to be carried.
        assert t.may_send_to("thread-1", "thread-1") is True

    def test_discord_refuses_a_thread_off_the_thread_roster(self) -> None:
        """Discord has TWO rosters; a thread route must consult the thread one.

        Removing a thread from the allow-list must stop its proactive traffic, and
        the user roster cannot answer that: a thread snowflake is not a user id. With
        no thread_id carried either, this is the exact shape a revoked thread's
        persisted link has.
        """
        t = self._discord(["42"], threads=["thread-9"])
        assert t.may_send_to("thread-1") is False
        assert t.may_send_to("thread-1", "thread-1") is False

    def test_discord_thread_route_ignores_the_user_roster(self) -> None:
        """An allow-listed USER must not wave through an unapproved thread.

        Otherwise a thread whose id was never approved becomes reachable purely
        because some user is on the DM roster.
        """
        t = self._discord(["thread-1"], threads=[])
        assert t.may_send_to("thread-1", "thread-1") is False

    def test_discord_outbound_matches_inbound_after_a_restart(self) -> None:
        """An auto-created thread is in-memory only, so both sides forget it.

        ``receive`` refuses inbound for a thread absent from the set; outbound must
        refuse too, or it becomes the more permissive of the two.
        """
        t = self._discord(["42"], threads=[])
        assert t.may_send_to("auto-thread-1", "auto-thread-1") is False
        # Registered the way the auto-thread path does it, it is reachable again.
        t._allowed_threads.add("auto-thread-1")
        assert t.may_send_to("auto-thread-1", "auto-thread-1") is True

    def test_discord_refuses_a_dm_it_cannot_identify(self) -> None:
        """No principal on a DM route means nothing left to consult.

        The DM channel id is opaque, so permitting here would wave through a
        recipient nobody can name. Affordable to refuse because the principal is
        also recovered for a unified bucket (see TestSessionPrincipalExtraction).
        """
        assert self._discord(["42"]).may_send_to("dm-chan-1") is False

    def test_discord_refuses_an_empty_conversation(self) -> None:
        assert self._discord(["42"]).may_send_to("", principal="42") is False

    def _webex(
        self,
        allowed: list[str],
        *,
        rooms: list[str] | None = None,
        group: bool = False,
    ) -> Any:
        from kiro_crew.webex.transport import WebexTransport

        return WebexTransport(
            object(),
            allowed_emails=allowed,
            allowed_room_ids=rooms or [],
            allow_group_rooms=group,
        )

    def test_webex_permits_an_allow_listed_principal_case_insensitively(self) -> None:
        t = self._webex(["alice@example.com"])
        assert t.may_send_to("room-1", principal="Alice@Example.com") is True

    def test_webex_refuses_a_revoked_principal(self) -> None:
        assert (
            self._webex(["bob@example.com"]).may_send_to("room-1", principal="alice@example.com")
            is False
        )

    def test_webex_refuses_a_room_it_cannot_identify(self) -> None:
        """No principal AND no space roster to fall back on leaves nothing to check."""
        assert self._webex(["alice@example.com"]).may_send_to("room-1") is False

    def test_webex_permits_an_allow_listed_space_with_no_principal(self) -> None:
        """A space route carries no principal, and refusing one is data loss.

        Its session key is ``forum``-namespaced on ``(chat_id, thread_id)``, so
        ``_session_principal`` names nobody -- by design, since the audience is a
        room. Answering only via the principal would therefore drop every
        proactive send into an allow-listed space: the dashboard mirror, cron
        results, subagent completions, compaction notices.
        """
        t = self._webex([], rooms=["space-1"], group=True)
        assert t.may_send_to("space-1") is True

    def test_webex_refuses_a_space_that_left_the_allow_list(self) -> None:
        """The link is persisted, so it outlives the roster that authorized it."""
        t = self._webex([], rooms=["space-1"], group=True)
        assert t.may_send_to("space-2") is False

    def test_webex_refuses_an_allow_listed_space_once_the_group_switch_is_off(self) -> None:
        """Both halves of the inbound gate, so outbound is never the looser one."""
        t = self._webex([], rooms=["space-1"], group=False)
        assert t.may_send_to("space-1") is False

    def test_webex_refuses_an_empty_conversation(self) -> None:
        t = self._webex(["alice@example.com"])
        assert t.may_send_to("", principal="alice@example.com") is False


class TestTransportsThatPermitWithAReason:
    """The remaining deferral stays permissive AND stays explicit."""

    @pytest.mark.parametrize("channel", sorted(_PERMITS_WITH_REASON))
    def test_the_override_exists_and_permits(self, channel: str) -> None:
        classes = _transport_classes()[channel]
        assert classes, f"{channel} has no transport class"
        for name in classes:
            assert _overrides_hook(channel, name), (
                f"{channel}.{name} is recorded as permitting with a reason, but inherits "
                f"the default instead of stating it"
            )

    def test_the_reason_is_recorded_in_the_docstring(self) -> None:
        """The row here and the code must not drift apart.

        A reader hitting the method needs the reason at the method, not only in
        this table -- so require a docstring that says it permits deliberately.
        """
        root = Path(kiro_crew_pkg.__file__).parent
        for channel in _PERMITS_WITH_REASON:
            source = (root / channel / "transport.py").read_text(encoding="utf-8")
            tree = ast.parse(source)
            docs = [
                ast.get_docstring(item) or ""
                for node in tree.body
                if isinstance(node, ast.ClassDef)
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == _HOOK
            ]
            assert docs, f"{channel}.{_HOOK} has no docstring"
            assert any(
                "permits" in doc.lower() for doc in docs
            ), f"{channel}.{_HOOK} must say that it permits deliberately, and why"


class TestSessionPrincipalExtraction:
    """``_session_principal``: empty rather than wrong, and never a guess."""

    def _principal(self, key: str) -> str:
        from kiro_crew.dashboard.chat_runner import _session_principal

        return _session_principal(key)

    def test_a_direct_dm_key_names_its_peer(self) -> None:
        assert self._principal("discord:kirocrew:direct:42") == "42"

    def test_a_generation_suffix_does_not_hide_the_peer(self) -> None:
        """A reset rotates the generation; the principal must survive it."""
        assert self._principal("discord:kirocrew:direct:42:gen3") == "42"

    def test_a_forum_route_names_no_single_principal(self) -> None:
        """Its audience is a room, so claiming a principal would be a lie.

        The scope is (chat_id, thread_id); returning chat_id would test a
        supergroup id against a USER roster.
        """
        assert self._principal("telegram:kirocrew:forum:-100123:7") == ""

    def test_a_unified_bucket_names_no_principal(self) -> None:
        """Channel and user drop out of a unified key by design.

        Deliberately NOT recovered from the session's stored attribution id, even
        though that value names a peer. It is written once at session creation while
        the origin/mirror link is rewritten on later turns, and a unified bucket
        collapses several peers into one session on purpose -- so the two drift, and
        authorizing against the attribution would check a DIFFERENT person than the
        link points at and pass. Wrong-and-passing is worse than declining to name
        one, which at least fails closed.
        """
        assert self._principal("unified:kirocrew") == ""
        assert self._principal("unified:kirocrew:gen2") == ""

    def test_a_webex_space_key_and_its_transport_agree(self) -> None:
        """Crosses the two modules that have to agree, using the REAL key builder.

        The defect this pins was invisible to either side alone: the dispatcher
        keys a space as a ``forum`` route (correctly -- its audience is a room, so
        it must not borrow a participant's DM key), ``_session_principal`` then
        names nobody (also correctly), and the transport refused for want of a
        principal -- silently dropping every proactive send into an allow-listed
        space. Only asking both questions in one test catches it, so the key is
        built with ``build_dm_session_key`` and the same ``space:``-prefixed route
        ``webex.transport_dispatch._route_of`` produces, rather than hand-written
        here where it could drift from the grammar.
        """
        from kiro_crew.messaging.link import CHAT_TYPE_FORUM, build_dm_session_key
        from kiro_crew.webex.transport import WebexTransport

        room = "Y2lzY29zcGFyazovL3Jvb20xMjM"
        key = build_dm_session_key("webex", "kirocrew", f"space:{room}", chat_type=CHAT_TYPE_FORUM)
        transport = WebexTransport(
            object(), allowed_emails=[], allowed_room_ids=[room], allow_group_rooms=True
        )

        assert self._principal(key) == ""
        assert transport.may_send_to(room, None, principal=self._principal(key)) is True

    def test_a_webex_unified_dm_stays_refused_by_the_space_arm(self) -> None:
        """The space roster must not become a way back into a unified bucket.

        A unified key names no principal either, so the space arm is the only thing
        standing between it and a permit -- and it holds because the route is a 1:1
        DM room, which no operator puts in the SPACE allow-list.
        """
        from kiro_crew.webex.transport import WebexTransport

        transport = WebexTransport(
            object(),
            allowed_emails=["alice@example.com"],
            allowed_room_ids=["space-1"],
            allow_group_rooms=True,
        )

        assert self._principal("unified:kirocrew") == ""
        assert transport.may_send_to("dm-room-1", principal="") is False

    def test_the_extractor_reads_only_the_key(self) -> None:
        """Pins the derivation as key-only, so no second record can creep back in.

        A signature taking session state is what made the unsound version possible;
        this asserts the function cannot consult one.
        """
        import inspect

        from kiro_crew.dashboard.chat_runner import _session_principal

        assert list(inspect.signature(_session_principal).parameters) == ["session_key"]

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "dashboard:main",
            "1234567890.123456",  # legacy bare Slack thread_ts
            "cron:job-1",
        ],
    )
    def test_a_key_outside_the_grammar_yields_nothing(self, key: str) -> None:
        assert self._principal(key) == ""


class _StubTransport:
    """A transport whose outbound-authz answer the test controls.

    *peers* is what its ``direct_peer_of`` attests for a conversation id: the
    ladder confirms a link's recorded principal against it before handing one in.
    """

    def __init__(self, permitted: bool | Exception, peers: dict[str, str] | None = None) -> None:
        self._permitted = permitted
        self._peers = dict(peers or {})
        self.capabilities = type("Caps", (), {"supports_proactive_send": True})()
        self.calls: list[tuple[str | None, str | None, str]] = []

    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        self.calls.append((conversation_id, thread_id, principal))
        if isinstance(self._permitted, Exception):
            raise self._permitted
        return self._permitted

    def direct_peer_of(self, conversation_id: str) -> str:
        return self._peers.get(conversation_id, "")


class _StubState:
    def __init__(self, transport: Any) -> None:
        self._transport = transport

    def get_channel_transport(self, channel_type: str) -> Any:
        return self._transport


class TestTheLadderConsultsTheTransport:
    """The chokepoint every proactive leg shares.

    Placed on ``_resolve_channel_target`` rather than at each caller because that
    is what makes the cron legs, the compaction notice, the mirror and subagent
    completion all inherit the check -- and what stops the next proactive leg from
    having to remember it.
    """

    @pytest.fixture(autouse=True)
    def _permit_governance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Isolate the recipient decision from the channel-scope decision."""
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: type("D", (), {"permitted": True})(),
        )

    def _resolve(
        self, transport: Any, link: ChannelLink, key: str = "telegram:kirocrew:direct:111"
    ) -> Any:
        from kiro_crew.dashboard.chat_runner import _resolve_channel_target

        return _resolve_channel_target(_StubState(transport), key, link)

    def test_a_permitted_recipient_resolves(self) -> None:
        transport = _StubTransport(True)
        link = ChannelLink(channel_type="telegram", channel_id="111")
        assert self._resolve(transport, link) == (link, transport)

    def test_a_refused_recipient_does_not_resolve(self) -> None:
        """The whole point: no target means no send, on every proactive leg."""
        transport = _StubTransport(False)
        link = ChannelLink(channel_type="telegram", channel_id="111")
        assert self._resolve(transport, link) is None

    def test_the_link_is_passed_through_verbatim(self) -> None:
        """Including thread_id, or a forum Topic would be judged as a DM."""
        transport = _StubTransport(True)
        link = ChannelLink(channel_type="telegram", channel_id="-100123", thread_id="7")
        self._resolve(transport, link, key="telegram:kirocrew:forum:-100123:7")
        # A forum key names no principal, so the transport is told so rather than
        # handed the supergroup id as if it were a person.
        assert transport.calls == [("-100123", "7", "")]

    def test_the_session_principal_reaches_the_transport(self) -> None:
        """Without this, Discord and Webex cannot reach their rosters at all."""
        transport = _StubTransport(True)
        link = ChannelLink(channel_type="discord", channel_id="dm-chan-1")
        self._resolve(transport, link, key="discord:kirocrew:direct:42")
        assert transport.calls == [("dm-chan-1", None, "42")]

    def test_the_link_principal_reaches_the_transport_for_a_dashboard_key(self) -> None:
        """A ``dashboard:chat-*`` key names nobody; the record the gateway admitted
        on the link does, and the transport's own pairing does not object.

        The record is signed under the gateway's admission key by the path that
        creates the link -- the dashboard link handler or the ``!sessions`` pick,
        through ``sign_mirror_admission`` -- and the map stores it verbatim beside
        the conversation id it describes; the map never signs. That is what lets a Discord DM mirror of a
        dashboard-born session pass the recipient leg -- with or without the
        transport having seen the DM in this process.
        """
        link = _admitted(
            "dashboard:chat-1-1700000000",
            ChannelLink(channel_type="discord", channel_id="dm-chan-1", principal="42"),
        )
        agreeing = _StubTransport(True, peers={"dm-chan-1": "42"})
        self._resolve(agreeing, link, key="dashboard:chat-1-1700000000")
        assert agreeing.calls == [("dm-chan-1", None, "42")]
        unaware = _StubTransport(True)
        self._resolve(unaware, link, key="dashboard:chat-1-1700000000")
        assert unaware.calls == [("dm-chan-1", None, "42")]

    def test_a_link_principal_the_gateway_did_not_admit_is_withheld(self) -> None:
        """The transport is told nobody, not the row's word: a row with no admission
        (a claim from something that is not the gateway), or one the transport's own
        record of the DM contradicts (a rewritten row)."""
        unsigned = ChannelLink(channel_type="discord", channel_id="dm-chan-1", principal="42")
        transport = _StubTransport(True, peers={"dm-chan-1": "42"})
        self._resolve(transport, unsigned, key="dashboard:chat-1-1700000000")
        assert transport.calls == [("dm-chan-1", None, "")]
        admitted = _admitted("dashboard:chat-1-1700000000", unsigned)
        contradicted = _StubTransport(True, peers={"dm-chan-1": "77"})
        self._resolve(contradicted, admitted, key="dashboard:chat-1-1700000000")
        assert contradicted.calls == [("dm-chan-1", None, "")]

    def test_the_key_outranks_the_link_principal(self) -> None:
        """A key that names a peer keeps its reading whatever the link records."""
        transport = _StubTransport(True, peers={"dm-chan-1": "77"})
        link = ChannelLink(channel_type="discord", channel_id="dm-chan-1", principal="77")
        self._resolve(transport, link, key="discord:kirocrew:direct:42")
        assert transport.calls == [("dm-chan-1", None, "42")]

    def test_an_explicit_principal_outranks_both(self) -> None:
        """The caller that already established the recipient is taken verbatim."""
        from kiro_crew.dashboard.chat_runner import _resolve_channel_target

        transport = _StubTransport(True)
        link = ChannelLink(channel_type="discord", channel_id="dm-chan-1", principal="77")
        _resolve_channel_target(
            _StubState(transport), "discord:kirocrew:direct:42", link, principal="5"
        )
        assert transport.calls == [("dm-chan-1", None, "5")]

    def test_a_raising_transport_fails_closed(self) -> None:
        """An allow-list check that errored has authorized nobody."""
        transport = _StubTransport(RuntimeError("roster unavailable"))
        link = ChannelLink(channel_type="telegram", channel_id="111")
        assert self._resolve(transport, link) is None

    def test_check_recipient_false_skips_only_the_recipient_leg(self) -> None:
        """The one caller whose link carries a CONFIGURED-TARGET id, not a
        conversation id (mirror-link creation), opts out: the recipient
        question is unanswerable in that spelling — ``user:123`` can never match
        a roster of bare ids — and is re-decided by that caller against the
        resolved id. Governance and capability still gate the resolve."""
        transport = _StubTransport(False)
        link = ChannelLink(channel_type="telegram", channel_id="user:123")
        from kiro_crew.dashboard.chat_runner import _resolve_channel_target

        resolved = _resolve_channel_target(
            _StubState(transport), "telegram:kirocrew:direct:123", link, check_recipient=False
        )
        assert resolved == (link, transport)
        # The leg was skipped, not consulted-and-ignored.
        assert transport.calls == []

    def test_the_recipient_leg_defaults_on(self) -> None:
        """Every persisted-link caller keeps the check without naming the flag."""
        transport = _StubTransport(False)
        link = ChannelLink(channel_type="telegram", channel_id="111")
        assert self._resolve(transport, link) is None
        assert transport.calls == [("111", None, "111")]

    def test_a_refusal_is_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A revoked recipient losing its notices must not look like an idle agent."""
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        link = ChannelLink(channel_type="telegram", channel_id="111")
        self._resolve(_StubTransport(False), link)
        assert len(recorded) == 1
        assert recorded[0]["outcome"] == "denied"
        assert recorded[0]["source"] == "telegram"
        assert recorded[0]["operation"] == "channel.proactive_send_authorize"


class _MirrorState(_StubState):
    """A state whose session store answers ``get_mirror_link`` with one link."""

    def __init__(self, transport: Any, link: ChannelLink | None) -> None:
        super().__init__(transport)
        self.sessions = type("Sessions", (), {"get_mirror_link": staticmethod(lambda key: link)})()


def _admitted(session_key: str, link: ChannelLink) -> ChannelLink:
    """*link* as the gateway stores it: signed for *session_key* and its location."""
    import dataclasses

    from kiro_crew.mirror_admission import sign_mirror_admission

    return dataclasses.replace(link, admission=sign_mirror_admission(session_key, link))


class TestADashboardSessionMirroredToADiscordDm:
    """The reported defect, decided by the REAL transports through the real ladder.

    A dashboard-born session's key (``dashboard:chat-<n>-<ts>``) names no peer, and
    a Discord DM channel id is unrelated to the user snowflake the roster holds, so
    a link that carried only the conversation gave the recipient leg nothing to
    consult: every dashboard-driven reply into the mirror was refused and dropped,
    silently, while the same link on Telegram delivered because there the
    conversation id IS the user id.

    The peer now comes from the record the GATEWAY wrote when it admitted the
    mirror -- ``ChannelLink.principal`` under ``ChannelLink.admission``, a MAC only
    the gateway can mint over the session key and the whole location. The session
    map is writable by in-sandbox code, so an unsigned or rewritten row fails to
    verify and is refused; the transport's own pairing (``direct_peer_of``) must
    agree whenever it has one; and the roster then decides -- per send -- whether the
    admitted peer is allow-listed.
    """

    _KEY = "dashboard:chat-1-1700000000"

    @pytest.fixture(autouse=True)
    def _permit_governance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: type("D", (), {"permitted": True})(),
        )

    @staticmethod
    def _discord(allowed: list[str], pairings: dict[str, str] | None = None) -> Any:
        """A real transport over a real client whose pairing store holds *pairings*."""
        from kiro_crew.discord.client import DiscordClient
        from kiro_crew.discord.transport import DiscordTransport

        client = DiscordClient(token="bot-secret")
        for channel_id, user_id in (pairings or {}).items():
            client.remember_dm_recipient(channel_id, user_id)
        return DiscordTransport(client, allowed_user_ids=allowed)

    @staticmethod
    def _telegram(allowed: list[str]) -> Any:
        from kiro_crew.telegram.transport import TelegramTransport

        return TelegramTransport(object(), allowed_user_ids=allowed)

    def _resolve(self, transport: Any, link: ChannelLink | None) -> Any:
        from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

        return _resolve_mirror_target(_MirrorState(transport, link), self._KEY)

    def test_a_genuine_link_delivers_right_after_a_restart(self) -> None:
        """The pairing store is empty -- the ordinary state after a gateway restart,
        before the peer has written anything -- and the admitted record alone
        carries the send: no inbound DM, nothing re-linked."""
        transport = self._discord(["42"])
        link = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        assert self._resolve(transport, link) == (link, transport)

    def test_a_peer_removed_from_the_roster_is_refused(self) -> None:
        """Revocation still lands: the record names the peer, the roster judges it."""
        transport = self._discord(["99"])
        link = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        assert self._resolve(transport, link) is None

    def test_a_row_rewritten_to_another_allowed_users_dm_is_refused(self) -> None:
        """The forgery the round before this one still admitted: a consistent
        rewrite of (channel_id, principal) to ANOTHER allow-listed user's DM, whose
        pairing the client even has cached. The MAC was minted over the original
        location, so the rewritten row does not verify and nothing else is asked."""
        transport = self._discord(["42", "55"], {"dm-chan-55": "55"})
        genuine = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        rewritten = ChannelLink(
            channel_type="discord",
            channel_id="dm-chan-55",
            principal="55",
            admission=genuine.admission,
        )
        assert self._resolve(transport, rewritten) is None

    def test_a_row_naming_an_allowed_peer_for_a_revoked_users_dm_is_refused(self) -> None:
        """The other forgery: a revoked user's DM channel with an allow-listed
        principal. Signed for the genuine location, the MAC does not cover the
        swapped channel id; and even a row the forger could not sign is caught
        twice, since the transport pairs that channel with the revoked user."""
        transport = self._discord(["42"], {"dm-chan-revoked": "77"})
        genuine = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        forged = ChannelLink(
            channel_type="discord",
            channel_id="dm-chan-revoked",
            principal="42",
            admission=genuine.admission,
        )
        assert self._resolve(transport, forged) is None

    def test_a_row_moved_to_another_session_is_refused(self) -> None:
        """The MAC binds the SESSION too: a valid record copied onto another
        session's row does not verify there, so one session's admission cannot be
        laundered into another's replies."""
        transport = self._discord(["42"])
        signed_elsewhere = _admitted(
            "dashboard:chat-2-1700000000",
            ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42"),
        )
        assert self._resolve(transport, signed_elsewhere) is None

    def test_a_row_without_an_admission_is_refused(self) -> None:
        """A principal with no MAC is a claim from something that is not the
        gateway. Refused, whatever the transport knows."""
        transport = self._discord(["42"], {"dm-chan-9": "42"})
        unsigned = ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        assert self._resolve(transport, unsigned) is None

    def test_a_transport_that_knows_another_peer_wins_over_the_record(self) -> None:
        """Defense in depth: when the client's own pairing names someone else for
        the conversation, the send is refused even though the record verifies."""
        transport = self._discord(["42"], {"dm-chan-9": "77"})
        link = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        assert self._resolve(transport, link) is None
        # And the same record is served once the pairing agrees.
        transport.client.remember_dm_recipient("dm-chan-9", "42")
        assert self._resolve(transport, link) == (link, transport)

    def test_a_rotated_signing_key_refuses_until_re_linked(self, monkeypatch) -> None:
        """A token-key rotation invalidates every admission at once; the mirror is
        refused until the session is re-linked, which mints a fresh record."""
        import secrets

        from kiro_crew.dashboard import token_secret

        transport = self._discord(["42"])
        link = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        assert self._resolve(transport, link) == (link, transport)
        rotated = secrets.token_bytes(32)
        monkeypatch.setattr(token_secret, "_get_secret", lambda: rotated)
        assert self._resolve(transport, link) is None
        relinked = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        )
        assert self._resolve(transport, relinked) == (relinked, transport)

    def test_a_refusal_is_audited_and_said_once(self, monkeypatch, caplog) -> None:
        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_RECIPIENT_LOGGED", set())
        transport = self._discord(["42"])
        unsigned = ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            assert self._resolve(transport, unsigned) is None
            assert self._resolve(transport, unsigned) is None
        admission_rows = [kw for kw in recorded if kw["operation"] == "channel.mirror_admission"]
        assert [kw["outcome"] for kw in admission_rows] == ["unverified", "unverified"]
        # The row's ids come from a file in-sandbox code can write; the two SEL fields
        # stored verbatim carry in-tree constants only, and the ids ride in the
        # redacted ``resources`` field.
        assert {kw["caller"] for kw in admission_rows} == {"cross-surface"}
        assert {kw["source"] for kw in admission_rows} == {"session_map"}
        assert all("dm-chan-9" in kw["resources"] for kw in admission_rows)
        said = [r for r in caplog.records if "no valid gateway admission" in r.getMessage()]
        assert len(said) == 1 and "Re-link" in said[0].getMessage()
        assert "dm-chan-9" not in said[0].getMessage() and "42" not in said[0].getMessage()

    def test_the_said_once_set_retains_a_fixed_size_digest_not_the_id(
        self, monkeypatch, caplog
    ) -> None:
        """The said-once set is capped at 512 entries, and a cap bounds every field it
        retains: a conversation id comes off a row in a file in-sandbox code can write
        at any length, so 512 refusals with successively larger ids would otherwise
        hold unbounded memory under the cap. Each entry is a fixed-length digest of
        the marker; the raw id is never retained, and distinct ids stay distinct."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: None})(),
        )
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_RECIPIENT_LOGGED", set())
        transport = self._discord(["42"])
        huge_a = "a" * (1 << 20)
        huge_b = "b" * (1 << 20)
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            for channel_id in (huge_a, huge_b, huge_a):
                unsigned = ChannelLink(
                    channel_type="discord", channel_id=channel_id, principal="42"
                )
                assert self._resolve(transport, unsigned) is None
        retained = chat_runner._RECIPIENT_LOGGED
        assert len(retained) == 2, "two distinct ids, two markers; the repeat adds nothing"
        assert all(isinstance(entry, str) and len(entry) == 64 for entry in retained), retained
        assert sum(len(entry) for entry in retained) < 1 << 12
        joined = "".join(retained)
        assert "aaaa" not in joined and "bbbb" not in joined
        # Distinct ids are still said once EACH, and the repeat is demoted to DEBUG.
        warned = [r for r in caplog.records if "no valid gateway admission" in r.getMessage()]
        assert [r.levelname for r in warned] == ["WARNING", "WARNING"]

    def test_a_link_that_records_no_peer_stays_refused(self) -> None:
        """A row that names no peer has nothing to admit: a link written before the
        peer was recorded, or a unified bucket bound from inside the channel."""
        transport = self._discord(["42"], {"dm-chan-9": "42"})
        link = ChannelLink(channel_type="discord", channel_id="dm-chan-9")
        assert self._resolve(transport, link) is None

    def test_an_ordinary_unlinks_displaced_link_still_resolves_its_recipient(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The unbind notice is sent AFTER the clear, to the link the clear displaced:
        by then the row is gone from the store. The recipient check judges the link it
        is handed -- the admission covers the session and the location, nothing the
        store has to be consulted for -- so a genuine, peer-named DM mirror the user
        simply unlinked resolves its recipient for the notice: no false `unverified`
        audit row, no WARNING blaming a rewritten row, and the said-once set is not
        latched for the conversation, so a later real forgery still warns.
        """
        import dataclasses
        from unittest.mock import patch

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_runner import _resolve_channel_target
        from kiro_crew.mirror_admission import sign_mirror_admission
        from kiro_crew.session_map import SessionMap, set_unbind_listener

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        monkeypatch.setattr(chat_runner, "_RECIPIENT_LOGGED", set())
        transport = self._discord(["42"])
        state = _StubState(transport)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sessions = SessionMap()
        state.sessions = sessions
        link = ChannelLink(channel_type="discord", channel_id="dm-chan-9", principal="42")
        signed = dataclasses.replace(link, admission=sign_mirror_admission(self._KEY, link))
        sessions.set_mirror_link(self._KEY, signed, accepts_inbound=True)
        displaced: list[ChannelLink] = []
        set_unbind_listener(lambda key, gone, reason: displaced.append(gone))
        try:
            with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
                assert sessions.clear_mirror_link(self._KEY) is True
                assert sessions.get_mirror_link(self._KEY) is None  # the row is gone
                assert len(displaced) == 1 and displaced[0].admission == signed.admission
                # What the unbind notice does with the displaced link, after the clear.
                resolved = _resolve_channel_target(state, self._KEY, displaced[0])
        finally:
            set_unbind_listener(None)
        assert resolved == (displaced[0], transport)
        assert [kw for kw in recorded if kw["operation"] == "channel.mirror_admission"] == []
        assert not [r for r in caplog.records if "no valid gateway admission" in r.getMessage()]
        # Not latched: the next genuine forgery at this conversation still warns.
        forged = ChannelLink(
            channel_type="discord", channel_id="dm-chan-9", principal="42", admission="f" * 64
        )
        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_runner"):
            assert _resolve_channel_target(state, self._KEY, forged) is None
        said = [r for r in caplog.records if "no valid gateway admission" in r.getMessage()]
        assert [r.levelname for r in said] == ["WARNING"]


class TestRecipientPrincipal:
    """``_recipient_principal``: the key first, then the gateway-admitted record --
    verified, and unopposed by the transport -- and never a guess."""

    _KEY = "dashboard:chat-1-1700000000"

    class _Attesting:
        def __init__(self, peers: dict[str, str]) -> None:
            self._peers = peers

        def direct_peer_of(self, conversation_id: str) -> str:
            return self._peers.get(conversation_id, "")

    def _principal(self, key: str, link: Any, transport: Any = None) -> str:
        from kiro_crew.dashboard.chat_runner import _recipient_principal

        return _recipient_principal(key, link, transport or self._Attesting({}))

    def test_the_key_wins_when_it_names_a_peer(self) -> None:
        link = ChannelLink(channel_type="discord", channel_id="dm-1", principal="77")
        transport = self._Attesting({"dm-1": "77"})
        assert self._principal("discord:kirocrew:direct:42", link, transport) == "42"

    def test_an_admitted_record_answers_when_the_key_names_nobody(self) -> None:
        link = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-1", principal="42")
        )
        assert self._principal(self._KEY, link) == "42"
        assert self._principal(self._KEY, link, self._Attesting({"dm-1": "42"})) == "42"

    def test_an_unverified_record_names_nobody(self) -> None:
        """No admission, an admission for another location, an admission for
        another session: each is a claim the gateway did not make for this row."""
        bare = ChannelLink(channel_type="discord", channel_id="dm-1", principal="42")
        assert self._principal(self._KEY, bare) == ""
        elsewhere = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-2", principal="42")
        )
        moved = ChannelLink(
            channel_type="discord", channel_id="dm-1", principal="42", admission=elsewhere.admission
        )
        assert self._principal(self._KEY, moved) == ""
        other_session = _admitted("dashboard:chat-2-1700000000", bare)
        assert self._principal(self._KEY, other_session) == ""

    def test_a_transport_that_contradicts_the_record_names_nobody(self) -> None:
        link = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-1", principal="42")
        )
        assert self._principal(self._KEY, link, self._Attesting({"dm-1": "77"})) == ""

    def test_nothing_named_yields_nothing(self) -> None:
        bare = ChannelLink(channel_type="discord", channel_id="dm-1")
        assert self._principal(self._KEY, bare) == ""
        assert self._principal("unified:kirocrew", bare) == ""
        # A link double that predates the field, no link at all, and a transport
        # without the hook all name nobody rather than raising in the send path.
        admitted = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-1", principal="42")
        )
        assert self._principal(self._KEY, object()) == ""
        assert self._principal(self._KEY, None) == ""
        assert self._principal(self._KEY, admitted, object()) == "42"

    def test_a_raising_hook_is_read_as_not_on_record(self) -> None:
        """An attestation that could not be read contradicts nothing; the verified
        record still answers (the hook is defense in depth, not the authority)."""

        class _Broken:
            def direct_peer_of(self, conversation_id: str) -> str:
                raise RuntimeError("pairing store unavailable")

        admitted = _admitted(
            self._KEY, ChannelLink(channel_type="discord", channel_id="dm-1", principal="42")
        )
        assert self._principal(self._KEY, admitted, _Broken()) == "42"

    def test_the_session_key_extractor_is_untouched(self) -> None:
        """The key-only derivation keeps its contract; the link is read beside it,
        not through it."""
        from kiro_crew.dashboard.chat_runner import _session_principal

        assert _session_principal(self._KEY) == ""
        assert _session_principal("discord:kirocrew:direct:42") == "42"


class TestMirrorAdmission:
    """``kiro_crew.mirror_admission``: the gateway's MAC over a principal-bearing row."""

    def test_signs_only_a_row_that_names_a_peer(self) -> None:
        from kiro_crew.mirror_admission import sign_mirror_admission

        assert (
            sign_mirror_admission("dashboard:chat-1", ChannelLink("discord", channel_id="dm-1"))
            == ""
        )
        signed = sign_mirror_admission(
            "dashboard:chat-1", ChannelLink("discord", channel_id="dm-1", principal="42")
        )
        assert len(signed) == 64 and int(signed, 16) >= 0

    @pytest.mark.parametrize(
        "planted",
        [
            {"admission": "\u00fc" * 64},
            {"admission": "\u00e9" + "a" * 63},
            {"admission": 123},
            {"admission": ["f" * 64]},
            {"admission": b"f" * 64},
            {"admission": "F" * 64},
            {"principal": ["42"]},
            {"principal": 42},
            {"channel_id": {"id": "dm-1"}},
            {"channel_id": 7},
            {"thread_id": object()},
            {"channel_type": None},
        ],
        ids=[
            "non-ascii-admission",
            "one-non-ascii-char",
            "int-admission",
            "list-admission",
            "bytes-admission",
            "uppercase-hex",
            "list-principal",
            "int-principal",
            "dict-channel-id",
            "int-channel-id",
            "object-thread-id",
            "none-channel-type",
        ],
    )
    def test_a_malformed_planted_row_refuses_and_never_raises(self, planted: dict) -> None:
        """The row comes from a file in-sandbox code can write, and this check sits on
        the send path of a dashboard turn: every shape it can take is a refusal, never
        an exception -- the constant-time comparison alone raises on a non-ASCII
        string, which would have aborted the turn on exactly the row it exists to
        reject. ``restorable_link`` strips such a row rather than raising too."""
        import dataclasses

        from kiro_crew.mirror_admission import restorable_link, verify_mirror_admission

        genuine = _admitted(
            "dashboard:chat-1", ChannelLink("discord", channel_id="dm-1", principal="42")
        )
        row = dataclasses.replace(genuine, **planted)
        assert verify_mirror_admission("dashboard:chat-1", row) is False
        restored = restorable_link("dashboard:chat-1", row)
        assert restored.principal is None and restored.admission is None

    @pytest.mark.parametrize(
        "field, value",
        [
            ("channel_type", "webex"),
            ("channel_id", "dm-2"),
            ("thread_id", "t-1"),
            ("principal", "77"),
        ],
    )
    def test_every_field_of_the_location_is_covered(self, field: str, value: str) -> None:
        import dataclasses

        from kiro_crew.mirror_admission import verify_mirror_admission

        original = ChannelLink("discord", channel_id="dm-1", thread_id=None, principal="42")
        signed = _admitted("dashboard:chat-1", original)
        assert verify_mirror_admission("dashboard:chat-1", signed) is True
        altered = dataclasses.replace(signed, **{field: value})
        assert verify_mirror_admission("dashboard:chat-1", altered) is False
        assert verify_mirror_admission("dashboard:chat-2", signed) is False

    def test_the_session_key_is_canonicalized_on_both_sides(self) -> None:
        """The map stores rows under the canonical key; the ladder is handed the
        session's key. Both spellings of one session verify the same record."""
        from kiro_crew.messaging.link import canonical_key
        from kiro_crew.mirror_admission import verify_mirror_admission

        raw = "dashboard:chat-1-1700000000"
        signed = _admitted(
            canonical_key(raw), ChannelLink("discord", channel_id="dm-1", principal="42")
        )
        assert verify_mirror_admission(raw, signed) is True

    def test_a_tampered_or_missing_admission_does_not_verify(self) -> None:
        from kiro_crew.mirror_admission import verify_mirror_admission

        signed = _admitted(
            "dashboard:chat-1", ChannelLink("discord", channel_id="dm-1", principal="42")
        )
        assert (
            verify_mirror_admission(
                "dashboard:chat-1",
                ChannelLink("discord", channel_id="dm-1", principal="42"),
            )
            is False
        )
        flipped = ("0" if signed.admission[0] != "0" else "1") + signed.admission[1:]  # type: ignore[index]
        assert (
            verify_mirror_admission(
                "dashboard:chat-1",
                ChannelLink("discord", channel_id="dm-1", principal="42", admission=flipped),
            )
            is False
        )
        assert (
            verify_mirror_admission(
                "dashboard:chat-1",
                ChannelLink("discord", channel_id="dm-1", principal="42", admission="not-hex"),
            )
            is False
        )

    def test_an_unreadable_key_admits_nobody(self, monkeypatch) -> None:
        from kiro_crew.dashboard import token_secret
        from kiro_crew.mirror_admission import verify_mirror_admission

        signed = _admitted(
            "dashboard:chat-1", ChannelLink("discord", channel_id="dm-1", principal="42")
        )

        def _boom() -> bytes:
            raise OSError("key store unreadable")

        monkeypatch.setattr(token_secret, "_get_secret", _boom)
        assert verify_mirror_admission("dashboard:chat-1", signed) is False

    def test_the_purpose_key_is_not_the_signing_secret_itself(self) -> None:
        """Domain separation: the admission key is derived from the token signing
        secret under a purpose label, so an admission is never a valid token MAC
        and a token is never a valid admission."""
        import hashlib
        import hmac

        from kiro_crew.dashboard import token_secret
        from kiro_crew.mirror_admission import _material, sign_mirror_admission

        link = ChannelLink("discord", channel_id="dm-1", principal="42")
        under_raw_secret = hmac.new(
            token_secret._get_secret(), _material("dashboard:chat-1", link), hashlib.sha256
        ).hexdigest()
        assert sign_mirror_admission("dashboard:chat-1", link) != under_raw_secret

    def test_only_the_two_authorized_creation_paths_mint_an_admission(self) -> None:
        """A generic writer that signed whatever it was handed would launder a forged
        row into a trusted one the moment any path re-set it -- a rollback, a
        restore, a migration. So the signer has exactly two call sites in the
        package: the dashboard mirror-link handler and the resume controller's pick
        commit, the two paths that authorize a peer for a conversation. Pinned by
        AST over every module, so a third writer is a named failure here rather than
        a quiet widening.
        """
        import ast as _ast

        import kiro_crew

        pkg = Path(kiro_crew.__file__).resolve().parent
        sites: set[tuple[str, str]] = set()
        for path in pkg.rglob("*.py"):
            if "_vendor" in path.parts or path.name == "mirror_admission.py":
                continue
            src = path.read_text(encoding="utf-8")
            if "sign_mirror_admission" not in src:
                continue
            tree = _ast.parse(src)
            parents: dict[_ast.AST, _ast.AST] = {}
            for node in _ast.walk(tree):
                for child in _ast.iter_child_nodes(node):
                    parents[child] = node
            for node in _ast.walk(tree):
                if not (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)):
                    continue
                if node.func.id != "sign_mirror_admission":
                    continue
                cur: _ast.AST | None = node
                while cur is not None and not isinstance(
                    cur, (_ast.FunctionDef, _ast.AsyncFunctionDef)
                ):
                    cur = parents.get(cur)
                fn = (
                    cur.name
                    if isinstance(cur, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                    else "<module>"
                )
                sites.add((path.relative_to(pkg).as_posix(), fn))
        assert sites == {
            ("dashboard/chat_mirror.py", "api_chat_slot_mirror_link"),
            ("messaging/session_resume.py", "commit_binding"),
        }, sites

    def test_the_map_itself_never_signs(self) -> None:
        """The store's writer carries bytes through; it does not import the signer."""
        import kiro_crew.session_map as session_map_mod

        src = Path(session_map_mod.__file__).read_text(encoding="utf-8")
        assert "sign_mirror_admission(" not in src

    def test_every_mirror_writer_is_a_signer_a_sanitized_rollback_or_peerless(self) -> None:
        """The convention every writer of a mirror row has to keep, made a named
        failure rather than a memory: a site that stores a link naming a peer either
        MINTS its admission (the two creation paths) or is a rollback that re-sets a
        row it read back through ``restorable_link``; every other site stores a
        location with no peer and no admission. The three sets are enumerated by AST
        and pinned, so a new ``set_mirror_link`` caller must be classified here --
        and a rollback that forgot ``restorable_link`` fails this test, not just an
        audit line.
        """
        import ast as _ast

        import kiro_crew

        pkg = Path(kiro_crew.__file__).resolve().parent
        signers: set[tuple[str, str]] = set()
        sanitized: set[tuple[str, str]] = set()
        others: set[tuple[str, str]] = set()
        for path in pkg.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            src = path.read_text(encoding="utf-8")
            if "set_mirror_link" not in src:
                continue
            tree = _ast.parse(src)
            parents: dict[_ast.AST, _ast.AST] = {}
            for node in _ast.walk(tree):
                for child in _ast.iter_child_nodes(node):
                    parents[child] = node
            for node in _ast.walk(tree):
                if not (
                    isinstance(node, _ast.Call)
                    and isinstance(node.func, _ast.Attribute)
                    and node.func.attr == "set_mirror_link"
                ):
                    continue
                cur: _ast.AST | None = node
                while cur is not None and not isinstance(
                    cur, (_ast.FunctionDef, _ast.AsyncFunctionDef)
                ):
                    cur = parents.get(cur)
                assert cur is not None, f"{path}: set_mirror_link at module level"
                site = (path.relative_to(pkg).as_posix(), cur.name)
                innermost_src = _ast.get_source_segment(src, cur) or ""
                # The enclosing chain, innermost first: the dashboard handler signs
                # in its body and stores from a nested claim closure, so the signer
                # test reads every enclosing function while the rollback discipline
                # is judged on the closure that performs the write.
                chain: list[str] = []
                scope: _ast.AST | None = cur
                while scope is not None:
                    if isinstance(scope, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        chain.append(_ast.get_source_segment(src, scope) or "")
                    scope = parents.get(scope)
                if "restorable_link(" in innermost_src:
                    sanitized.add(site)
                elif any("sign_mirror_admission(" in fn_src for fn_src in chain):
                    signers.add(site)
                else:
                    others.add(site)
        assert signers == {
            ("dashboard/chat_mirror.py", "_claim_binding"),
            ("messaging/session_resume.py", "commit_binding"),
        }, signers
        assert sanitized == {
            ("dashboard/chat_mirror.py", "_release_binding"),
            ("messaging/session_resume.py", "release"),
            ("messaging/session_resume.py", "rollback_binding"),
        }, sanitized
        # Peer-less writers: the origin binds and in-channel links (a bare location),
        # and the manager's passthrough. None of these may grow a principal without
        # moving into one of the two sets above.
        assert others == {
            ("messaging/link.py", "bind_origin_mirror"),
            ("messaging/link.py", "rebind_conversation_location"),
            ("session.py", "set_mirror_link"),
            ("webex/transport_dispatch.py", "_rebind"),
            ("wecom/transport_dispatch.py", "_handle_link"),
        }, others

    def test_restorable_link_carries_a_verified_row_and_strips_an_unverified_one(
        self, monkeypatch, caplog
    ) -> None:
        """What a rollback may put back: a row whose admission verifies goes back
        verbatim; a row naming a peer under a missing or wrong admission goes back
        as its location alone, audited once, so the send is refused rather than a
        forged row laundered into a trusted one."""
        from kiro_crew.mirror_admission import restorable_link

        recorded: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "kiro_crew.mirror_admission.sel",
            lambda: type("S", (), {"log_api_access": lambda self, **kw: recorded.append(kw)})(),
        )
        key = "dashboard:chat-1-1700000000"
        bare = ChannelLink("discord", channel_id="dm-1")
        assert restorable_link(key, bare) is bare
        signed = _admitted(key, ChannelLink("discord", channel_id="dm-1", principal="42"))
        assert restorable_link(key, signed) is signed
        forged = ChannelLink("discord", channel_id="dm-1", principal="42", admission="f" * 64)
        unsigned = ChannelLink("discord", channel_id="dm-1", principal="42")
        moved = _admitted("dashboard:chat-2-1700000000", unsigned)
        with caplog.at_level("WARNING", logger="kiro_crew.mirror_admission"):
            for row in (forged, unsigned, moved):
                restored = restorable_link(key, row)
                assert restored == bare
                assert restored.principal is None and restored.admission is None
        assert [kw["outcome"] for kw in recorded] == ["stripped_on_restore"] * 3
        assert {kw["caller"] for kw in recorded} == {"mirror-rollback"}
        assert {kw["source"] for kw in recorded} == {"session_map"}
        said = [r for r in caplog.records if "restoring the binding without it" in r.getMessage()]
        assert len(said) == 3 and all("dm-1" not in r.getMessage() for r in said)
        # A row whose ids are attacker-shaped never reaches the verbatim fields or the
        # log line; only the redacted ``resources`` field carries them.
        planted = ChannelLink(
            "AKIAIOSFODNN7EXAMPLE",
            channel_id="ghp_" + "b" * 36,
            principal="42",
            admission="f" * 64,
        )
        recorded.clear()
        with caplog.at_level("WARNING", logger="kiro_crew.mirror_admission"):
            restorable_link(key, planted)
        assert recorded[0]["caller"] == "mirror-rollback" and recorded[0]["source"] == "session_map"
        assert "ghp_" not in recorded[0]["caller"] + recorded[0]["source"]
        assert all(
            "ghp_" not in r.getMessage() and "AKIA" not in r.getMessage() for r in caplog.records
        )


def _send_returns_only_empty(channel: str, class_name: str) -> bool:
    """Whether *class_name*'s ``send_message`` can ONLY return an empty string.

    That is the observable form of "this platform gives me no message id": every
    return is the constant ``""``, so the value cannot express failure and failure
    has to raise instead.
    """
    path = Path(kiro_crew_pkg.__file__).parent / channel / "transport.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.AsyncFunctionDef) or item.name != "send_message":
                continue
            returns = [n.value for n in ast.walk(item) if isinstance(n, ast.Return)]
            if not returns:
                return False
            return all(
                isinstance(r, ast.Constant) and r.value == "" for r in returns if r is not None
            )
    return False


# Channels whose send returns an id only when the platform reports one, so an
# empty id is still success and failure raises. The structural check above cannot
# see that (the id comes from one frame deeper), so their implementation is held
# to a different check: send_message hands the client's result straight back, and
# the client's own send is pinned behaviourally below.
_BEST_EFFORT_ID_CHANNELS = {"imessage"}


def _send_only_returns_client_send(channel: str, class_name: str) -> bool:
    """Whether every ``return`` in *class_name*'s ``send_message`` is ``await self._client.send(...)``.

    That shape is what lets the client's contract stand for the transport's: no
    branch can swallow a failure into its own empty string or invent an id.
    """
    path = Path(kiro_crew_pkg.__file__).parent / channel / "transport.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if not isinstance(item, ast.AsyncFunctionDef) or item.name != "send_message":
                continue
            handlers = [n for n in ast.walk(item) if isinstance(n, ast.ExceptHandler)]
            returns = [n.value for n in ast.walk(item) if isinstance(n, ast.Return)]
            if handlers or not returns:
                return False
            return all(
                isinstance(r, ast.Await)
                and isinstance(r.value, ast.Call)
                and ast.unparse(r.value.func) == "self._client.send"
                for r in returns
            )
    return False


def _declares_no_message_id(channel: str) -> bool:
    """Whether this channel's module-level ``TransportCapabilities(...)`` says False.

    Read from the SOURCE, like everything else in this file: importing a transport
    pulls in its client's optional dependencies, and the transport that fails to
    import is exactly the one whose gap would go unnoticed. An absent keyword means
    the dataclass default, which is True.
    """
    path = Path(kiro_crew_pkg.__file__).parent / channel / "transport.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "TransportCapabilities":
            continue
        for kw in node.keywords:
            if kw.arg == "returns_message_id":
                return isinstance(kw.value, ast.Constant) and kw.value.value is False
    return False


class TestTheMessageIdConventionIsDeclared:
    """``returns_message_id`` must match what ``send_message`` can actually return.

    Two conventions exist: an empty id means REFUSED on most platforms, and means
    SUCCESS on the two that carry no id at all. A caller cannot guess, so it asks
    the capability -- and a capability that disagrees with the code is worse than no
    capability, because every proactive-send site now trusts it.
    """

    def test_every_transport_agrees_with_its_own_send(self) -> None:
        mismatched: list[str] = []
        for channel, classes in sorted(_transport_classes().items()):
            declared_idless = _declares_no_message_id(channel)
            for class_name in classes:
                only_empty = _send_returns_only_empty(channel, class_name)
                if channel in _BEST_EFFORT_ID_CHANNELS:
                    # Its id comes from the client, best-effort: the transport must
                    # hand that result straight back, and the client's raise-on-
                    # failure contract is pinned by the behavioural test below.
                    if not (
                        declared_idless and _send_only_returns_client_send(channel, class_name)
                    ):
                        mismatched.append(
                            f"{channel}.{class_name}: a best-effort-id channel must "
                            "declare returns_message_id=False and return only "
                            "`await self._client.send(...)` from send_message"
                        )
                    continue
                if only_empty != declared_idless:
                    mismatched.append(
                        f"{channel}.{class_name}: declares returns_message_id="
                        f"{not declared_idless} while send_message "
                        + ("only ever returns an empty string" if only_empty else "returns an id")
                    )
        assert not mismatched, (
            "these transports' declared id convention contradicts their send_message: "
            f"{mismatched}"
        )

    def test_the_two_id_less_transports_are_the_ones_that_declare_it(self) -> None:
        """Named, not counted: a count passes when one channel swaps for another.

        WeCom's proactive command and Feishu's reply are the two platforms that
        answer a send with nothing. Both must therefore RAISE on failure, which is
        what makes "nothing raised" a delivery signal for them.
        """
        idless = {
            channel
            for channel, classes in _transport_classes().items()
            for class_name in classes
            if _send_returns_only_empty(channel, class_name)
        }
        assert idless == {"wecom", "feishu"}
        assert all(_declares_no_message_id(channel) for channel in idless)

    def test_the_declared_id_less_set_is_named(self) -> None:
        declared = {channel for channel in _transport_classes() if _declares_no_message_id(channel)}
        assert declared == {"wecom", "feishu"} | _BEST_EFFORT_ID_CHANNELS

    def test_imessage_reads_an_empty_id_as_delivered(self) -> None:
        # The bridge reports the GUID best-effort, so a delivered send can answer
        # with "". Failure raises out of IMessageClient.send instead.
        from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
        from kiro_crew.messaging.transport import delivery_confirmed

        assert delivery_confirmed(IMESSAGE_CAPABILITIES, "")
        assert delivery_confirmed(IMESSAGE_CAPABILITIES, "guid-1")

    @pytest.mark.asyncio
    async def test_imessage_client_send_returns_empty_only_on_success_and_raises_on_failure(
        self, tmp_path: Path
    ) -> None:
        # What makes returns_message_id=False safe for iMessage: "" comes back only
        # from a send the bridge answered, and every failure raises.
        from kiro_crew.imessage.client import IMessageClient
        from kiro_crew.imessage.rpc import RpcError, RpcTransportError

        replies: list[Any] = []

        async def fake_call(method: str, params: dict[str, Any], **_kw: Any) -> Any:
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        imc = IMessageClient(cursor_path=tmp_path / "c.json")
        imc._call = fake_call  # type: ignore[method-assign]

        replies.append({})
        assert await imc.send("+15550100", "no guid") == ""
        replies.append({"guid": "g-1"})
        assert await imc.send("+15550100", "with guid") == "g-1"
        for failure in (
            RpcError(-32602, "invalid params"),
            RpcError(-32001, "delivery uncertain"),
            RpcTransportError("bridge exited"),
        ):
            replies.append(failure)
            with pytest.raises(type(failure)):
                await imc.send("+15550100", "fails")

    @pytest.mark.parametrize(
        "channel", ["telegram", "discord", "slack", "teams", "webex", "whatsapp"]
    )
    def test_an_id_bearing_transport_keeps_the_strict_reading(self, channel: str) -> None:
        # The other direction, per channel: these DO return an id, so an empty one
        # must stay a failure for them. Declaring False here would silently turn
        # every refused send into a reported success.
        assert not _declares_no_message_id(channel)
