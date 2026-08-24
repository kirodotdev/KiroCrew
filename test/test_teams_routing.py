"""Teams proactive routing: learning an address, and forgetting a dead one.

The Bot Framework offers no way to LOOK UP a conversation's ``serviceUrl``, so the
only copy is the one we recorded. Two consequences, one per direction:

* Until something records it, no cron result, mirror leg or ``send_message`` tool
  can reach that person -- so a promptless install/join activity, which carries the
  whole routable tuple under the same JWT attestation as a message, is learned.
* Once a user blocks the bot or removes the app the route is permanently dead, and
  keeping it turns every later send into a red badge with nothing able to clear it.

Both paths re-apply the SAME authorization the message path does, which is what
keeps "reachable" from meaning "authorized".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kiro_crew.teams.client import TeamsClient, TeamsSendError
from kiro_crew.teams.transport import TeamsTransport

_SVC = "https://smba.trafficmanager.net/"
_ALICE = "alice@example.com"


class _Client:
    def __init__(self, *, fail_status: int = 0) -> None:
        self.sent: list[str] = []
        self.fail_status = fail_status

    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        if self.fail_status:
            raise TeamsSendError(f"HTTP {self.fail_status}", status=self.fail_status)
        self.sent.append(content)
        return "mid-1"


def _transport(client: Any, tmp_path: Any, monkeypatch: Any) -> TeamsTransport:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return TeamsTransport(client, allowed_emails=[_ALICE], dispatch=None)


class TestForgetADeadRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [403, 404])
    async def test_a_permanently_refused_conversation_loses_its_route(
        self, tmp_path, monkeypatch, status
    ) -> None:
        """403 is what Teams answers once the user blocked the bot or removed the app."""
        transport = _transport(_Client(fail_status=status), tmp_path, monkeypatch)
        await transport._store.ensure_loaded()
        transport._store.remember("conv-1", _SVC, identity=_ALICE)
        assert transport.configured_targets()[0].available is True

        with pytest.raises(TeamsSendError):
            await transport.send_message("conv-1", "hi")

        assert transport.service_url_for("conv-1") == ""
        assert transport.configured_targets()[0].available is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 502, 0])
    async def test_a_transient_failure_keeps_the_route(self, tmp_path, monkeypatch, status) -> None:
        """Dropping a route on a hiccup would make the outage look permanent."""
        transport = _transport(_Client(fail_status=status or 500), tmp_path, monkeypatch)
        await transport._store.ensure_loaded()
        transport._store.remember("conv-1", _SVC, identity=_ALICE)

        with pytest.raises(TeamsSendError):
            await transport.send_message("conv-1", "hi")

        assert transport.service_url_for("conv-1") == _SVC

    @pytest.mark.asyncio
    async def test_forgetting_survives_a_restart(self, tmp_path, monkeypatch) -> None:
        """The drop is persisted, or the next process advertises the dead route again."""
        transport = _transport(_Client(fail_status=403), tmp_path, monkeypatch)
        await transport._store.ensure_loaded()
        transport._store.remember("conv-1", _SVC, identity=_ALICE)
        await transport._store.flush()

        with pytest.raises(TeamsSendError):
            await transport.send_message("conv-1", "hi")

        fresh = _transport(_Client(), tmp_path, monkeypatch)
        await fresh._store.ensure_loaded()
        assert fresh.service_url_for("conv-1") == ""


class TestLearnARouteWithoutAPrompt:
    @pytest.mark.asyncio
    async def test_an_install_makes_the_target_reachable_before_any_message(
        self, tmp_path, monkeypatch
    ) -> None:
        transport = _transport(_Client(), tmp_path, monkeypatch)
        await transport._store.ensure_loaded()
        assert transport.configured_targets()[0].available is False

        await transport.note_route("conv-1", "personal", _SVC, _ALICE)

        assert transport.configured_targets()[0].available is True
        assert await transport.resolve_configured_target(f"user:{_ALICE}") == ("conv-1", None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ctype", ["channel", "groupChat", ""])
    async def test_a_non_personal_join_records_nothing(self, tmp_path, monkeypatch, ctype) -> None:
        """Same fail-closed scope as ``receive``: a reply in a channel would leak."""
        transport = _transport(_Client(), tmp_path, monkeypatch)

        await transport.note_route("conv-1", ctype, _SVC, _ALICE)

        assert transport.service_url_for("conv-1") == ""

    @pytest.mark.asyncio
    async def test_a_stranger_records_nothing(self, tmp_path, monkeypatch) -> None:
        """ "Reachable" must never come to mean something other than "authorized"."""
        transport = _transport(_Client(), tmp_path, monkeypatch)

        await transport.note_route("conv-1", "personal", _SVC, "mallory@example.com")

        assert transport.service_url_for("conv-1") == ""


class TestTheClientOnlyOffersAttestedRoutes:
    @staticmethod
    def _client(learned: list[tuple[str, str, str, str]]) -> TeamsClient:
        class _V:
            def verify(self, token: str) -> dict:
                return {"aud": "app", "serviceurl": _SVC}

        client = TeamsClient(app_id="app", app_password="pw", validator=_V())  # type: ignore

        async def _note(conv: str, ctype: str, url: str, identity: str) -> None:
            learned.append((conv, ctype, url, identity))

        client.on_route = _note
        return client

    @staticmethod
    def _activity(atype: str, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": atype,
            "id": "act-1",
            "channelId": "msteams",
            "serviceUrl": _SVC,
            "from": {"aadObjectId": "aad-1", "userPrincipalName": _ALICE},
            "conversation": {"id": "conv-1", "conversationType": "personal"},
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    @pytest.mark.parametrize("atype", ["conversationUpdate", "installationUpdate"])
    async def test_a_promptless_install_or_join_offers_its_route(self, atype) -> None:
        learned: list[tuple[str, str, str, str]] = []
        client = self._client(learned)

        await client._dispatch_activity(self._activity(atype), {"serviceurl": _SVC})

        assert learned == [("conv-1", "personal", _SVC, _ALICE)]

    @pytest.mark.asyncio
    async def test_a_promptless_activity_never_drives_a_turn(self) -> None:
        """It carries an address, not a prompt; running a turn would answer nothing."""
        learned: list[tuple[str, str, str, str]] = []
        client = self._client(learned)
        turns: list[Any] = []

        async def handler(inbound: Any) -> None:
            turns.append(inbound)

        client.set_message_handler(handler)
        await client._dispatch_activity(self._activity("conversationUpdate"), {"serviceurl": _SVC})

        assert learned, "the route was learned"
        assert turns == [], "and no turn ran"

    @pytest.mark.asyncio
    async def test_a_foreign_channel_offers_no_route(self) -> None:
        """The channel check runs AHEAD of this, so Direct Line cannot seed a route."""
        learned: list[tuple[str, str, str, str]] = []
        client = self._client(learned)

        await client._dispatch_activity(
            self._activity("conversationUpdate", channelId="directline"), {"serviceurl": _SVC}
        )

        assert learned == []

    @pytest.mark.asyncio
    async def test_an_unattested_serviceurl_offers_no_route(self) -> None:
        """Otherwise a replay could point every later proactive send at its own host."""
        learned: list[tuple[str, str, str, str]] = []
        client = self._client(learned)

        await client._dispatch_activity(
            self._activity("conversationUpdate", serviceUrl="https://attacker.example/"),
            {"serviceurl": _SVC},
        )

        assert learned == []

    @pytest.mark.asyncio
    async def test_an_unrelated_activity_type_offers_no_route(self) -> None:
        learned: list[tuple[str, str, str, str]] = []
        client = self._client(learned)

        for atype in ("typing", "messageReaction", "invoke", "endOfConversation"):
            await client._dispatch_activity(self._activity(atype), {"serviceurl": _SVC})

        assert learned == []


class TestTheCredentialOnlyGoesToConnectorHosts:
    """The persisted route is a FILE, so its contents are not attested.

    The inbound path binds `serviceUrl` to the JWT's own `serviceurl` claim precisely
    so a replayed activity cannot redirect the app bearer token — but that attestation
    does not survive persistence. This store lives under the data home, so an injected
    agent with write access could otherwise write `https://attacker.example`, wait for
    a restart, and collect the Connector token from the first proactive send. Requiring
    a Microsoft-operated Connector host costs nothing legitimate (a real serviceUrl is
    always one) and closes every supplier of a bad URL at once.
    """

    @pytest.mark.parametrize(
        "url,allowed",
        [
            ("https://smba.trafficmanager.net/amer/", True),
            ("https://europe.smba.trafficmanager.net/", True),
            ("https://x.botframework.com/", True),
            # Trailing FQDN root dot is the same host to every resolver.
            ("https://smba.trafficmanager.net./", True),
            ("https://attacker.example/", False),
            ("http://smba.trafficmanager.net/", False),
            # Dot-anchored, so a lookalike prefix cannot satisfy the suffix match.
            ("https://botframework.com.evil.example/", False),
            # A Connector endpoint has no port but 443.
            ("https://smba.trafficmanager.net:8443/", False),
            ("", False),
            ("not a url", False),
        ],
    )
    def test_the_predicate_is_deny_by_default(self, url: str, allowed: bool) -> None:
        from kiro_crew.teams.client import connector_host_allowed

        assert connector_host_allowed(url) is allowed

    @pytest.mark.asyncio
    async def test_a_poisoned_row_does_not_survive_a_reload(self, tmp_path, monkeypatch) -> None:
        """The attack the gate exists for: write the file, wait for a restart."""
        import json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        transport = _transport(_Client(), tmp_path, monkeypatch)
        path = transport._store.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "conversations": {
                        "conv-good": {"service_url": _SVC, "seen_at": 2.0},
                        "conv-evil": {"service_url": "https://attacker.example/", "seen_at": 3.0},
                    },
                    "identities": {_ALICE: "conv-evil"},
                }
            ),
            encoding="utf-8",
        )

        await transport._store.ensure_loaded()

        assert transport.service_url_for("conv-good") == _SVC
        assert transport.service_url_for("conv-evil") == "", "a non-Connector host is dropped"
        # And the identity row pointing at it advertises nothing reachable.
        assert transport.configured_targets()[0].available is False

    @pytest.mark.asyncio
    async def test_the_send_refuses_it_too(self, tmp_path, monkeypatch) -> None:
        """The store is defence in depth; the CHOKEPOINT is where the token is attached."""
        from kiro_crew.teams.client import TeamsClient

        client = TeamsClient(app_id="app", app_password="pw")
        client._token = "app-token"
        client._token_expiry = 1e18
        with pytest.raises(TeamsSendError, match="Connector hosts"):
            await client.send_message("conv-1", "hi", "https://attacker.example/")

    @pytest.mark.asyncio
    async def test_a_non_connector_host_is_never_recorded(self, tmp_path, monkeypatch) -> None:
        transport = _transport(_Client(), tmp_path, monkeypatch)
        await transport._store.ensure_loaded()

        assert transport._store.remember("conv-1", "https://attacker.example/") is False
        assert transport.service_url_for("conv-1") == ""


class TestTheRoutingStoreIsOnTheKeystoneFloor:
    """An agent may neither read nor write the file that decides where sends go.

    ``connector_host_allowed`` answers "may this HOST see our Connector token?", and it
    cannot answer "is this the right CONVERSATION?" -- one legitimate Connector host
    serves everybody. The identity -> conversation row is delivery addressing, so an
    agent able to rewrite it could point an operator's UPN at somebody else's chat and
    have the next cron result or ``send_message`` land there. The file therefore sits on
    the same read+write floor as every other keystone leaf.
    """

    def test_the_store_path_is_sensitive_both_ways(self) -> None:
        from pathlib import Path

        from kiro_crew.security import is_sensitive_path
        from kiro_crew.teams.service_urls import STORE_DIRNAME, STORE_FILENAME

        for prefix in (".kiro/crew", ".kirocrew"):
            path = Path.home() / prefix / STORE_DIRNAME / STORE_FILENAME
            assert is_sensitive_path(str(path)), f"{prefix} not covered"

    def test_the_default_path_is_the_one_the_leaf_registers(self, tmp_path, monkeypatch) -> None:
        """The protection and the store have to agree on WHERE the file is.

        They are stated in two modules, so a move in one without the other silently
        un-fences the file while every other test keeps passing.
        """
        from kiro_crew.security import is_sensitive_path
        from kiro_crew.teams.service_urls import ServiceUrlStore

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        store = ServiceUrlStore()

        assert store.path().parent.name == "routing"
        # And the same layout under a real home is what the leaf covers.
        assert is_sensitive_path(str(Path.home() / ".kiro/crew" / store.path().parent.name))

    def test_the_atomic_write_temp_sibling_is_covered_too(self) -> None:
        """The DIRECTORY is registered, not the file, and this is why.

        ``atomic_write`` publishes through a ``tempfile.mkstemp`` sibling in the same
        parent. A file leaf matches only its exact name, so with the store loose in the
        data-home root an agent could overwrite that temp file in the window before the
        rename and have the rename publish routing of its choosing.
        """
        from pathlib import Path

        from kiro_crew.security import is_sensitive_path
        from kiro_crew.teams.service_urls import STORE_DIRNAME

        temp = Path.home() / ".kiro/crew" / STORE_DIRNAME / "tmpAb3Kd9Zq.tmp"
        assert is_sensitive_path(str(temp))

    def test_a_shell_write_to_the_store_is_refused(self) -> None:
        from kiro_crew.security import is_sensitive_bash_command
        from kiro_crew.teams.service_urls import STORE_DIRNAME, STORE_FILENAME

        target = f"~/.kiro/crew/{STORE_DIRNAME}/{STORE_FILENAME}"
        for command in (
            f"cat {target}",
            f"echo '{{}}' > {target}",
            f"tee {target}",
            # The temp sibling the rename would publish.
            f"echo '{{}}' > ~/.kiro/crew/{STORE_DIRNAME}/tmpAb3Kd9Zq.tmp",
        ):
            assert is_sensitive_bash_command(command) is not None, command

    @pytest.mark.asyncio
    async def test_the_store_itself_still_reads_and_writes(self, tmp_path) -> None:
        """The gate is for AGENT tools. The store opens its own path directly."""
        from kiro_crew.teams.service_urls import ServiceUrlStore

        path = tmp_path / "routing" / "teams_service_urls.json"
        store = ServiceUrlStore(path=path)
        await store.ensure_loaded()
        assert store.remember("conv-1", _SVC, identity=_ALICE) is True
        await store.flush()

        reloaded = ServiceUrlStore(path=path)
        await reloaded.ensure_loaded()
        assert reloaded.conversation_for(_ALICE) == "conv-1"


class TestWhichIdentityTheAllowListAuthorizes:
    """Teams may send a UPN, an AAD object id, or BOTH, and the list may hold either.

    So the resolution picks the form the list actually AUTHORIZES. A fixed
    email-first order denies a user whose object id is listed whenever Teams also
    sends an email -- the ordinary shape for a guest account and for any tenant that
    allow-lists by object id.
    """

    _OID = "11111111-2222-3333-4444-555555555555"

    def _inbound(self, *, email: str = "", object_id: str = "") -> Any:
        from kiro_crew.teams.client import TeamsInbound

        return TeamsInbound(
            conversation_id="conv-1",
            conversation_type="personal",
            service_url=_SVC,
            text="hi",
            user_email=email,
            aad_object_id=object_id,
        )

    def _transport_for(self, allowed: list[str], tmp_path, monkeypatch) -> TeamsTransport:
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        return TeamsTransport(_Client(), allowed_emails=allowed, dispatch=None)

    def test_an_allow_listed_object_id_wins_over_an_unlisted_email(
        self, tmp_path, monkeypatch
    ) -> None:
        transport = self._transport_for([self._OID], tmp_path, monkeypatch)

        resolved = transport._resolve_identity(
            self._inbound(email="guest#ext#", object_id=self._OID)
        )

        assert resolved == self._OID

    def test_the_email_still_wins_when_both_are_listed(self, tmp_path, monkeypatch) -> None:
        """An install listing both forms keeps the session key it already had."""
        transport = self._transport_for([_ALICE, self._OID], tmp_path, monkeypatch)

        resolved = transport._resolve_identity(self._inbound(email=_ALICE, object_id=self._OID))

        assert resolved == _ALICE

    def test_an_unauthorized_sender_is_named_the_recognisable_way(
        self, tmp_path, monkeypatch
    ) -> None:
        """Neither form is listed, so the deny audit gets the human-readable one."""
        transport = self._transport_for([_ALICE], tmp_path, monkeypatch)

        resolved = transport._resolve_identity(
            self._inbound(email="mallory@example.com", object_id=self._OID)
        )

        assert resolved == "mallory@example.com"

    @pytest.mark.asyncio
    async def test_the_dispatcher_gets_the_form_the_gate_authorized(
        self, tmp_path, monkeypatch
    ) -> None:
        """One decision, carried -- not re-derived downstream from a fixed order.

        The gate admits this user on their object id; a dispatcher that re-derived the
        identity would key their session on the unlisted UPN instead, so their turns would
        persist under a session nobody authorized and owner-only `/sessions` would refuse
        the very user just let in.
        """
        from kiro_crew.teams.transport_dispatch import TeamsDispatcher

        seen: list[str] = []

        async def _dispatch(inbound: Any) -> None:
            seen.append(TeamsDispatcher._identity(inbound))

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        transport = TeamsTransport(_Client(), allowed_emails=[self._OID], dispatch=_dispatch)

        await transport.receive(self._inbound(email="guest#EXT#@x.com", object_id=self._OID))

        assert seen == [self._OID]

    @pytest.mark.asyncio
    async def test_a_turn_and_a_card_click_agree_on_the_session(
        self, tmp_path, monkeypatch
    ) -> None:
        """Two readers of the identity is two chances to disagree, and they did.

        The turn keyed on the UPN while a card click keyed on the object id, so the
        approval card resolved against a session nothing was awaiting and expired, and
        `/new` rotated a generation the turn was not using.
        """
        from kiro_crew.teams.transport_dispatch import TeamsDispatcher

        keys: list[str] = []

        async def _dispatch(inbound: Any) -> None:
            keys.append(TeamsDispatcher._identity(inbound))

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        transport = TeamsTransport(_Client(), allowed_emails=[self._OID], dispatch=_dispatch)
        inbound = self._inbound(email="guest#EXT#@x.com", object_id=self._OID)

        await transport.receive(inbound)

        # What `handle_message` keys the turn on, and what `_handle_card_action` keys a
        # click on, are the same call -- so this asserts they cannot drift apart.
        assert keys == [self._OID]

    def test_neither_form_present_resolves_to_nothing(self, tmp_path, monkeypatch) -> None:
        transport = self._transport_for([_ALICE], tmp_path, monkeypatch)

        assert transport._resolve_identity(self._inbound()) == ""
