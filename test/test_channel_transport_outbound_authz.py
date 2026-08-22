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
    "webex": "session binds room_id while the roster holds emails",
}

#: Transports that permit unconditionally, with the reason stated at the method.
_PERMITS_WITH_REASON = {
    "slack": "ladder returns early for SLACK_NAMESPACE; never consulted",
}


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
        """A stale row would silently excuse a channel that no longer exists."""
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
        # Learned while Alice was allowed; she is no longer on the roster.
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

        return DiscordTransport(
            object(), allowed_user_ids=allowed, allowed_thread_ids=threads or []
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

    def _webex(self, allowed: list[str]) -> Any:
        from kiro_crew.webex.transport import WebexTransport

        return WebexTransport(object(), allowed_emails=allowed)

    def test_webex_permits_an_allow_listed_principal_case_insensitively(self) -> None:
        t = self._webex(["alice@example.com"])
        assert t.may_send_to("room-1", principal="Alice@Example.com") is True

    def test_webex_refuses_a_revoked_principal(self) -> None:
        assert (
            self._webex(["bob@example.com"]).may_send_to("room-1", principal="alice@example.com")
            is False
        )

    def test_webex_refuses_a_room_it_cannot_identify(self) -> None:
        """Direct-rooms-only, so every route has exactly one recipient."""
        assert self._webex(["alice@example.com"]).may_send_to("room-1") is False


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
    """A transport whose outbound-authz answer the test controls."""

    def __init__(self, permitted: bool | Exception) -> None:
        self._permitted = permitted
        self.capabilities = type("Caps", (), {"supports_proactive_send": True})()
        self.calls: list[tuple[str | None, str | None, str]] = []

    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        self.calls.append((conversation_id, thread_id, principal))
        if isinstance(self._permitted, Exception):
            raise self._permitted
        return self._permitted


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

    def test_a_raising_transport_fails_closed(self) -> None:
        """An allow-list check that errored has authorized nobody."""
        transport = _StubTransport(RuntimeError("roster unavailable"))
        link = ChannelLink(channel_type="telegram", channel_id="111")
        assert self._resolve(transport, link) is None

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

    @pytest.mark.parametrize(
        "channel", ["telegram", "discord", "slack", "teams", "webex", "whatsapp", "imessage"]
    )
    def test_an_id_bearing_transport_keeps_the_strict_reading(self, channel: str) -> None:
        # The other direction, per channel: these DO return an id, so an empty one
        # must stay a failure for them. Declaring False here would silently turn
        # every refused send into a reported success.
        assert not _declares_no_message_id(channel)
