"""Tests for the channel-addressed send leg of the messaging handlers.

This is an EGRESS surface reachable from the dashboard AND from the LLM (the MCP
``send_message`` tool), so the properties that matter are the four fail-closed
gates and — the one a caller cannot check for itself — that a send which did not
arrive is never reported as delivered.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard.handlers import messaging as mod


class _Caps:
    def __init__(self, proactive: bool = True, mention_grammars: bool = True) -> None:
        self.supports_proactive_send = proactive
        self.max_message_chars = 4000
        self.max_message_bytes = 0
        self.streaming = False
        self.edit = False
        # Mirrors the real default: defang unless the platform declares it parses
        # no broadcast-mention grammar.
        self.mention_grammars = mention_grammars


class _Transport:
    """Records sends and replays a scripted result per part."""

    def __init__(
        self,
        results: list[Any] | None = None,
        proactive: bool = True,
        mention_grammars: bool = True,
    ) -> None:
        self.capabilities = _Caps(proactive, mention_grammars)
        self.sent: list[tuple[str, str, Any]] = []
        self._results = results

    async def resolve_configured_target(self, target_id: str):
        return (f"room-for-{target_id}", None) if target_id != "gone" else None

    async def send_message(self, conversation_id: str, text: str, thread_id=None):
        self.sent.append((conversation_id, text, thread_id))
        if self._results is None:
            return "MSG1"
        return self._results.pop(0) if self._results else "MSG1"


def _state(**transports) -> Any:
    return SimpleNamespace(channel_transports=dict(transports))


async def _body(resp) -> dict:
    import json

    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def _permit(monkeypatch: pytest.MonkeyPatch):
    """Governance allows by default; a test that cares overrides it."""
    monkeypatch.setattr(mod, "_vet_channel_send", lambda *_a, **_kw: "")


class TestDeliveryHonesty:
    @pytest.mark.asyncio
    async def test_a_send_the_channel_did_not_accept_is_a_502(self) -> None:
        """A transport reports failure by RETURNING a falsy id, not by raising.

        Reading only exceptions answers 200 "ok" for a message that never
        arrived — worse than an error, because the caller (including the LLM,
        which cannot see the room) records it as delivered and moves on.
        """
        transport = _Transport(results=[None])
        resp = await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "hi"
        )

        assert resp.status == 502
        assert (await _body(resp))["code"] == "channel_delivery_failed"

    @pytest.mark.asyncio
    async def test_a_later_part_failing_is_also_a_502(self) -> None:
        # A partial delivery is still not a delivery: the caller asked for one
        # message and the room holds half of it.
        transport = _Transport(results=["MSG1", ""])
        long_text = "x" * 5000
        resp = await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", long_text
        )

        assert resp.status == 502
        assert len(transport.sent) == 2

    @pytest.mark.asyncio
    async def test_a_raising_transport_is_the_same_502(self) -> None:
        class _Boom(_Transport):
            async def send_message(self, *_a, **_kw):
                raise RuntimeError("connection reset")

        resp = await mod._send_to_channel_target(
            _state(webex=_Boom()), "webex", "user:a@b.com", "hi"
        )

        assert resp.status == 502

    @pytest.mark.asyncio
    async def test_an_accepted_send_reports_the_part_count(self) -> None:
        transport = _Transport()
        resp = await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "hi"
        )

        assert resp.status == 200
        assert await _body(resp) == {"ok": True, "delivered_to": "webex", "parts": 1}


class TestGates:
    @pytest.mark.asyncio
    async def test_an_unregistered_channel_is_refused(self) -> None:
        # Membership in the registry, never `channel_type != "slack"`: a negation
        # hands every channel added later whatever this path grants.
        resp = await mod._send_to_channel_target(_state(), "webex", "user:a@b.com", "hi")

        assert resp.status == 404
        assert (await _body(resp))["code"] == "channel_not_connected"

    @pytest.mark.asyncio
    async def test_a_channel_that_cannot_originate_is_refused(self) -> None:
        # WeCom's reply is bound to an inbound token, so saying so beats a
        # confusing platform error.
        resp = await mod._send_to_channel_target(
            _state(wecom=_Transport(proactive=False)), "wecom", "x", "hi"
        )

        assert resp.status == 400
        assert (await _body(resp))["code"] == "channel_no_proactive_send"

    @pytest.mark.asyncio
    async def test_a_governance_denial_refuses_before_any_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "_vet_channel_send", lambda *_a, **_kw: "profile denies webex")
        transport = _Transport()

        resp = await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "hi"
        )

        assert resp.status == 403
        assert (await _body(resp))["code"] == "channel_denied"
        assert transport.sent == []

    @pytest.mark.asyncio
    async def test_a_target_that_no_longer_resolves_is_refused(self) -> None:
        """The opaque id travelled through the browser or the model.

        The config may have narrowed since it was minted, so the channel's own
        allow-list is re-applied rather than the id being trusted as given.
        """
        transport = _Transport()
        resp = await mod._send_to_channel_target(_state(webex=transport), "webex", "gone", "hi")

        assert resp.status == 403
        assert (await _body(resp))["code"] == "target_not_allowed"
        assert transport.sent == []


class TestRedaction:
    @pytest.mark.asyncio
    async def test_credentials_are_redacted_before_the_wire(self) -> None:
        # The text can come from the model, and this leg bypasses the driver's
        # own stream redaction.
        transport = _Transport()
        await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "key AKIAIOSFODNN7EXAMPLE here"
        )

        assert "AKIAIOSFODNN7EXAMPLE" not in transport.sent[0][1]

    @pytest.mark.asyncio
    async def test_a_delimiter_split_credential_is_redacted_too(self) -> None:
        """A byte-level scan cannot see this one.

        The platform's own renderer strips the delimiters and reassembles the key,
        so the text is scanned in its DISPLAYED form — the difference between a
        byte redactor pair and the shared display sink.
        """
        transport = _Transport()
        await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "AKIA**IOSF**ODNN7EXAMPLE"
        )

        assert "AKIAIOSFODNN7EXAMPLE" not in transport.sent[0][1].replace("*", "")

    @pytest.mark.asyncio
    async def test_broadcast_mentions_are_defanged_where_the_platform_parses_them(self) -> None:
        # This leg is channel-NEUTRAL, and Slack/Discord DO have broadcast
        # grammars, so the defang is correct against a transport declaring one.
        transport = _Transport(mention_grammars=True)
        await mod._send_to_channel_target(
            _state(slack=transport), "slack", "user:a@b.com", "@everyone look"
        )

        assert "@everyone" not in transport.sent[0][1]

    @pytest.mark.asyncio
    async def test_an_email_survives_on_a_platform_with_no_broadcast_grammar(self) -> None:
        """The defang is not free, and Webex is where it costs something.

        Webex has no broadcast grammar AND its allow-list IS email addresses, so a
        ZWSP after every ``@`` makes every address the agent prints uncopyable.
        Declared on the transport (``mention_grammars=False``) rather than branched
        on a channel name here, so this leg stays channel-neutral.
        """
        transport = _Transport(mention_grammars=False)
        await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "mail kyle@example.com"
        )

        body = transport.sent[0][1]
        assert "kyle@example.com" in body
        assert "\u200b" not in body

    @pytest.mark.asyncio
    async def test_credentials_are_still_redacted_without_the_defang(self) -> None:
        """Skipping the defang must not skip the display-form credential scan."""
        transport = _Transport(mention_grammars=False)
        await mod._send_to_channel_target(
            _state(webex=transport), "webex", "user:a@b.com", "AKIA**IOSF**ODNN7EXAMPLE"
        )

        assert "AKIAIOSFODNN7EXAMPLE" not in transport.sent[0][1].replace("*", "")


class TestAuditing:
    @pytest.mark.asyncio
    async def test_a_target_denial_is_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refusal that leaves no record is the one an operator cannot review.

        Someone probing target ids would otherwise look identical to normal
        traffic on an egress chokepoint the model can reach.
        """
        calls: list[dict] = []

        class _SelSpy:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(mod, "_sel", lambda: _SelSpy())
        await mod._send_to_channel_target(_state(webex=_Transport()), "webex", "gone", "hi")

        assert [c["outcome"] for c in calls] == ["denied"]
        assert "target_not_configured" in calls[0]["resources"]

    @pytest.mark.asyncio
    async def test_a_delivery_failure_is_audited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []

        class _SelSpy:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(mod, "_sel", lambda: _SelSpy())
        await mod._send_to_channel_target(
            _state(webex=_Transport(results=[None])), "webex", "user:a@b.com", "hi"
        )

        assert [c["outcome"] for c in calls] == ["error"]

    @pytest.mark.asyncio
    async def test_an_accepted_send_is_audited_as_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []

        class _SelSpy:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(mod, "_sel", lambda: _SelSpy())
        await mod._send_to_channel_target(_state(webex=_Transport()), "webex", "user:a@b.com", "hi")

        assert [c["outcome"] for c in calls] == ["allowed"]


class TestGovernanceIdentity:
    """The ``channels`` re-vet resolves the CALLER's surface, not the target's.

    A subject synthesized from the destination (``webex:send_message``) would
    resolve the destination channel's own profile and make every per-surface
    operator binding inert on this leg — a cron restricted to Slack could still
    egress to Webex, and a profile bound to ``webex`` would wrongly block a
    dashboard operator's proactive send.
    """

    @pytest.mark.asyncio
    async def test_a_cron_caller_is_vetted_under_its_own_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            mod, "_vet_channel_send", lambda channel, session_key: seen.append(session_key) or ""
        )
        await mod._send_to_channel_target(
            _state(webex=_Transport()), "webex", "user:a@b.com", "hi", caller_session="cron:job1"
        )
        assert seen == ["cron:job1"]

    @pytest.mark.asyncio
    async def test_a_non_cron_caller_is_vetted_under_its_own_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fail-closed re-vet must resolve the CALLER's profile, not the host's.
        # Filtering the forwarded identity to ``cron:`` left a non-cron caller
        # vetted as HOST -- a permissive default that, paired with the MCP-side
        # fail-OPEN channel vet, was a governance bypass.
        seen: list[str] = []
        monkeypatch.setattr(
            mod, "_vet_channel_send", lambda channel, session_key: seen.append(session_key) or ""
        )
        await mod._send_to_channel_target(
            _state(webex=_Transport()),
            "webex",
            "user:a@b.com",
            "hi",
            caller_session="webex:kirocrew:direct:U9",
        )
        assert seen == ["webex:kirocrew:direct:U9"]

    @pytest.mark.asyncio
    async def test_a_callerless_send_degrades_to_the_host_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        monkeypatch.setattr(
            mod, "_vet_channel_send", lambda channel, session_key: seen.append(session_key) or ""
        )
        await mod._send_to_channel_target(_state(webex=_Transport()), "webex", "user:a@b.com", "hi")
        # Never the destination-derived ``webex:send_message`` that resolved the
        # wrong profile before this was fixed.
        assert seen == [mod.HOST_SESSION_KEY]
