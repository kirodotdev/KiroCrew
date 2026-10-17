"""WebexDispatcher's slash commands: ``/yolo``, ``/kirocrew dashboard``, ``/sessions``, ``/model``
and ``/agent``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from webex_dispatch_helpers import (
    _EMAIL,
    FakeClient,
    FakeConvLog,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _cfg,
    _cfg_group,
    _dispatcher,
    _inbound,
    _space,
)

from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent
from kiro_crew.webex import transport_dispatch as webex_dispatch
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.commands import build_help_text, parse_command
from kiro_crew.webex.transport_dispatch import WebexDispatcher

# ------------------------------------------------------------------
# /yolo — the global auto-approve grant
# ------------------------------------------------------------------


class TestYolo:
    @staticmethod
    def _grant(active: bool = False):
        so = mock.MagicMock()
        so.is_active.return_value = active
        so.activate.return_value = SimpleNamespace(active=True)
        so.renew.return_value = SimpleNamespace(renewed=True)
        return so

    @pytest.mark.asyncio
    async def test_a_bare_yolo_reports_status_and_usage(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=self._grant()):
            await d.handle_message(_inbound("/yolo"))
        reply = d.client.sent[-1][1]
        assert "OFF" in reply and "on | off | renew" in reply

    @pytest.mark.asyncio
    async def test_yolo_on_activates_and_audits(self) -> None:
        """Credential-shaped grants MUST be audited.

        A grant that auto-approves every tool is exactly the event an incident
        review needs to find, and the channel is where it was taken.
        """
        so = self._grant()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            with mock.patch.object(webex_dispatch, "sel") as sel_mock:
                await d.handle_message(_inbound("/yolo on"))
        so.activate.assert_called_once_with("webex")
        assert "YOLO ON" in d.client.sent[-1][1]
        op = sel_mock.return_value.log_api_access.call_args.kwargs
        assert op["operation"] == "webex.yolo_mode"
        assert op["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_yolo_on_when_already_active_does_not_re_activate(self) -> None:
        so = self._grant(active=True)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            await d.handle_message(_inbound("/yolo on"))
        so.activate.assert_not_called()
        assert "already ON" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_refused_activation_is_reported_as_denied(self) -> None:
        so = self._grant()
        so.activate.return_value = SimpleNamespace(active=False)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            with mock.patch.object(webex_dispatch, "sel") as sel_mock:
                await d.handle_message(_inbound("/yolo on"))
        assert "Couldn't turn YOLO on" in d.client.sent[-1][1]
        assert sel_mock.return_value.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_yolo_off_is_unconditional(self) -> None:
        """Deactivate runs even when the grant already lapsed.

        It also zeroes a lapsed grant's deadline, which closes the renew grace
        window so a later "/yolo renew" cannot resurrect it — and it records the
        operator's decision either way.
        """
        so = self._grant(active=False)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            await d.handle_message(_inbound("/yolo off"))
        so.deactivate.assert_called_once_with("webex")
        assert "YOLO OFF" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_renewing_an_inactive_grant_says_so(self) -> None:
        so = self._grant()
        so.renew.return_value = SimpleNamespace(renewed=False)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            await d.handle_message(_inbound("/yolo renew"))
        assert "not active" in d.client.sent[-1][1]


# ------------------------------------------------------------------
# /kirocrew dashboard — presigned login link
# ------------------------------------------------------------------


class TestDashboardLink:
    #: A stub token at the REAL length. A live token is ~218 characters (base64url
    #: payload + HMAC signature), and the exfiltration redactor fires on a query
    #: string past 200 — so a short stub asserts the link is delivered while every
    #: real link is redacted into an unusable one.
    TOKEN = "a1b2c3d4" * 27

    @pytest.mark.parametrize("origin", ["http://localhost:8765", "http://127.0.0.1:8765"])
    @pytest.mark.asyncio
    async def test_a_link_is_minted_capped_and_audited(self, origin: str) -> None:
        """Both origins, because only one of them exposed the bug.

        The exfiltration redactor flags an IP-literal host and not ``localhost``,
        so a test pinned to the default fallback origin passed while every
        operator who set ``dashboard.url`` to an IP got an unusable link.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.cfg.dashboard = SimpleNamespace(url=origin)

        with mock.patch(
            "kiro_crew.dashboard.token_auth.generate_token", return_value=self.TOKEN
        ) as gen:
            with mock.patch.object(webex_dispatch, "sel") as sel_mock:
                await d.handle_message(_inbound("/kirocrew dashboard 2h"))

        assert gen.call_args.kwargs["ttl_seconds"] == 7200
        # The WHOLE token, not a prefix: a redacted link would still contain
        # "token=" and the failure is that it does not authenticate.
        assert f"token={self.TOKEN}" in d.client.sent[-1][1]
        op = sel_mock.return_value.log_api_access.call_args.kwargs
        assert op["operation"] == "webex.dashboard_token"
        assert op["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_a_non_direct_room_is_refused(self) -> None:
        """A presigned link is a credential.

        The direct-room assertion is kept local rather than inherited from the
        transport's gate: that gate is a property of another layer, which a future
        group capability could relax.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        inbound = WebexInbound(
            person_email=_EMAIL, room_id="ROOM", text="/kirocrew dashboard", room_type="group"
        )

        with mock.patch("kiro_crew.dashboard.token_auth.generate_token") as gen:
            await d._handle_dashboard(inbound)

        gen.assert_not_called()
        assert "direct message" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_bare_kirocrew_answers_with_usage(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/kirocrew"))
        assert "Usage:" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_token_failure_reports_the_type_not_the_message(self) -> None:
        # The exception message can embed the dashboard URL or a response body.
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.cfg.dashboard = SimpleNamespace(url="http://localhost:8765")

        with mock.patch(
            "kiro_crew.dashboard.token_auth.generate_token",
            side_effect=RuntimeError("secret=abc"),
        ):
            await d.handle_message(_inbound("/kirocrew dashboard"))

        reply = d.client.sent[-1][1]
        assert "RuntimeError" in reply and "secret=abc" not in reply

    @pytest.mark.parametrize(
        "arg,secs",
        [("dashboard 2h", 7200), ("dashboard 30m", 1800), ("dashboard 90m", 5400)],
    )
    def test_a_valid_duration_comes_from_the_shared_grammar(self, arg: str, secs: int) -> None:
        """The grammar belongs to ``token_auth.parse_duration``, not this channel.

        Only the token SPLITTING is local, so a unit added to the shared parser
        reaches this command instead of diverging silently.
        """
        from kiro_crew.dashboard.token_auth import parse_duration

        assert parse_duration(webex_dispatch._ttl_spec(arg)) == secs

    @pytest.mark.parametrize(
        "arg",
        ["dashboard", "dashboard xx", "dashboard -1h", "dashboard 5d", "dashboard 2hh"],
    )
    @pytest.mark.asyncio
    async def test_an_unusable_duration_falls_back_to_the_default(self, arg: str) -> None:
        """The user asked for a link; refusing one over a typo trades a working
        link for a lecture. The shared parser returns None and the call site
        supplies the default."""
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.cfg.dashboard = SimpleNamespace(url="http://localhost:8765")

        with mock.patch("kiro_crew.dashboard.token_auth.generate_token", return_value="TOK") as gen:
            await d.handle_message(_inbound(f"/kirocrew {arg}"))

        assert gen.call_args.kwargs["ttl_seconds"] == 3600

    def test_the_ttl_is_clamped_to_the_server_maximum(self) -> None:
        # A user-supplied duration must not be able to widen the login window.
        from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, parse_duration

        assert parse_duration(webex_dispatch._ttl_spec("dashboard 9999h")) == MAX_SESSION_TTL_SECS

    @pytest.mark.parametrize(
        "secs,shown", [(3600, "1h"), (7200, "2h"), (1800, "30m"), (5400, "1h 30m"), (30, "1m")]
    )
    def test_ttl_display(self, secs: int, shown: str) -> None:
        assert webex_dispatch._format_ttl(secs) == shown


# ------------------------------------------------------------------
# Tests: /sessions
# ------------------------------------------------------------------


class FakeListingLog(FakeConvLog):
    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self._rows = rows

    def list_sessions(self) -> list[dict]:
        return list(self._rows)


class TestSessionsCommand:
    @staticmethod
    def _stem(d: WebexDispatcher, route: str, gen: int = 0) -> str:
        from kiro_crew.history import transcript_stem

        key = d._session_key(route)
        return transcript_stem(f"{key}:gen{gen}" if gen else key)

    @pytest.mark.asyncio
    async def test_it_lists_this_conversations_generations(self) -> None:
        """Regression: ``list_sessions`` reports a FILENAME STEM.

        Every character outside ``[\\w\\-.]`` is folded to ``_``, so a raw prefix
        test against a colon-bearing session key matches nothing at all — and the
        command silently reports "no conversations" forever.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        rows = [
            {"key": self._stem(d, _EMAIL, 2), "title": "newer"},
            {"key": self._stem(d, _EMAIL), "title": "first"},
        ]
        d.conv_log = FakeListingLog(rows)

        await d.handle_message(_inbound("/sessions"))

        body = d.client.sent[-1][1]
        assert "newer" in body and "first" in body

    @pytest.mark.asyncio
    async def test_repeated_new_does_not_materialize_empty_history_rows(self) -> None:
        log = FakeListingLog([])
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), conv_log=log)

        await d.handle_message(_inbound("/new"))
        await d.handle_message(_inbound("/new"))

        assert log.list_sessions() == []
        assert len(sessions.reserved_generations) == 2

    @pytest.mark.asyncio
    async def test_another_users_conversations_are_not_listed(self) -> None:
        """A title is the opening words of a message.

        Scoping to the caller's own bucket is what makes the audience of the list
        exactly the audience of the conversations in it.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        rows = [
            {"key": self._stem(d, _EMAIL), "title": "mine"},
            {"key": self._stem(d, "other@example.com"), "title": "theirs"},
        ]
        d.conv_log = FakeListingLog(rows)

        await d.handle_message(_inbound("/sessions"))

        body = d.client.sent[-1][1]
        assert "mine" in body and "theirs" not in body

    @pytest.mark.asyncio
    async def test_a_space_lists_the_spaces_own_conversations(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient(), cfg=_cfg_group())
        route = webex_dispatch._route_of(_space())
        rows = [
            {"key": self._stem(d, route), "title": "in the space"},
            {"key": self._stem(d, _EMAIL), "title": "my private dm"},
        ]
        d.conv_log = FakeListingLog(rows)

        await d.handle_message(_space("/sessions"))

        body = d.client.sent[-1][1]
        assert "in the space" in body and "my private dm" not in body

    @pytest.mark.asyncio
    async def test_the_current_conversation_is_marked(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.conv_log = FakeListingLog([{"key": self._stem(d, _EMAIL), "title": "here"}])

        await d.handle_message(_inbound("/sessions"))

        assert "current" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_empty_list_says_so(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.conv_log = FakeListingLog([])
        await d.handle_message(_inbound("/sessions"))
        assert "No earlier conversations" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_no_history_store_degrades_with_a_notice(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/sessions"))
        assert "not available" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_listing_failure_is_reported_rather_than_raised(self) -> None:
        class Boom(FakeConvLog):
            def list_sessions(self):
                raise OSError("disk")

        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.conv_log = Boom()
        await d.handle_message(_inbound("/sessions"))
        assert "Couldn't read" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_long_list_is_capped_with_a_remainder_line(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        total = webex_dispatch._SESSIONS_LIST_MAX + 3
        d.conv_log = FakeListingLog(
            [{"key": self._stem(d, _EMAIL, i), "title": f"t{i}"} for i in range(1, total + 1)]
        )

        await d.handle_message(_inbound("/sessions"))

        assert "and 3 older" in d.client.sent[-1][1]


# ------------------------------------------------------------------
# Tests: /model
# ------------------------------------------------------------------


class FakeAcpClient:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.set_calls: list[str] = []
        self._raises = raises

    async def set_model(self, model_id: str) -> None:
        if self._raises is not None:
            raise self._raises
        self.set_calls.append(model_id)


class OlderAcpClient:
    """A backend whose client predates ``session/set_model``.

    A separate class rather than a flag, because the code probes for the METHOD:
    a stub that exists and refuses would not exercise the branch.
    """

    set_calls: list[str] = []


class ModelProvider(FakeProvider):
    def __init__(
        self,
        models: list[dict] | None = None,
        *,
        has_set_model: bool = True,
        raises: Exception | None = None,
    ) -> None:
        super().__init__([AcpEvent(kind=EVENT_COMPLETE)])
        self._models = models if models is not None else [{"modelId": "m-1", "name": "Model One"}]
        self.client: object = FakeAcpClient(raises=raises) if has_set_model else OlderAcpClient()

    def available_models(self) -> list[dict]:
        return list(self._models)


class TestModelCommand:
    @pytest.mark.asyncio
    async def test_the_list_comes_from_what_the_backend_advertised(self) -> None:
        """Never a static catalogue.

        Accounts differ in entitlement, so a hardcoded list offers models the
        account cannot reach — which surfaces as a refusal mid-conversation.
        """
        d = _dispatcher(FakeSessions(ModelProvider()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        body = d.client.sent[-1][1]
        assert "Auto" in body and "Model One" in body

    @pytest.mark.asyncio
    async def test_auto_is_offered_once(self) -> None:
        provider = ModelProvider([{"modelId": "auto", "name": "Auto"}, {"modelId": "m-1"}])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        assert d.client.sent[-1][1].count("Auto") == 2  # the header line + one row

    @pytest.mark.asyncio
    async def test_no_session_yet_says_to_send_a_message_first(self) -> None:
        d = _dispatcher(FakeSessions(None, has_session=False), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        assert "No model list yet" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_out_of_range_pick_is_refused(self) -> None:
        d = _dispatcher(FakeSessions(ModelProvider()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 99"))
        assert "Pick a number between 1 and 2" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_non_numeric_pick_is_refused(self) -> None:
        d = _dispatcher(FakeSessions(ModelProvider()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model sonnet"))
        assert "Pick a number" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_pick_switches_the_live_conversation(self) -> None:
        """``session/set_model`` carries the conversation across.

        Recording the preference and telling the user to `/new` would make the
        command useless for the case people actually use it in: mid-conversation.
        """
        provider = ModelProvider()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        assert provider.client.set_calls == ["m-1"]
        assert "Now using" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_the_switch_takes_the_session_semaphore(self) -> None:
        # The switch and a turn share one stdio channel, so interleaving JSON-RPC
        # on it would corrupt both.
        provider = ModelProvider()
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        key = d._session_key(_EMAIL)
        await d.handle_message(_inbound("/model 2"))
        assert sessions.acquired == [key] and sessions.released == [key]

    @pytest.mark.asyncio
    async def test_a_busy_session_defers_rather_than_claiming_a_switch(self) -> None:
        provider = ModelProvider()
        sessions = FakeSessions(provider, acquire=False)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        assert provider.client.set_calls == []
        assert "a reply is still running" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_backend_without_set_model_defers(self) -> None:
        provider = ModelProvider(has_set_model=False)
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        assert "applies to your next one" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_failed_switch_is_reported_honestly(self) -> None:
        provider = ModelProvider(raises=RuntimeError("nope"))
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        body = d.client.sent[-1][1]
        assert "Couldn't switch" in body and "RuntimeError" in body

    @pytest.mark.asyncio
    async def test_picking_auto_records_without_claiming_a_live_switch(self) -> None:
        """ "Auto" has no ACP id meaning "let the backend choose"."""
        provider = ModelProvider()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 1"))
        assert provider.client.set_calls == []
        assert "applies to your next one" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_the_preference_reaches_the_next_turn(self) -> None:
        provider = ModelProvider()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        models: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            models.append(turn.model)

        await d.handle_message(_inbound("/model 2"))
        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_inbound("hello"))

        assert models == ["m-1"]

    @pytest.mark.asyncio
    async def test_a_backend_that_advertises_nothing_still_offers_auto(self) -> None:
        class Broken(ModelProvider):
            def available_models(self):
                raise RuntimeError("no")

        d = _dispatcher(FakeSessions(Broken()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        assert "Auto" in d.client.sent[-1][1]


# ------------------------------------------------------------------
# Tests: /agent (show and switch the conversation's agent)
# ------------------------------------------------------------------


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
    @pytest.mark.asyncio
    async def test_agent_name_switches_the_next_turn(self) -> None:
        sessions = FakeSessions(FakeProvider([AcpEvent(kind=EVENT_COMPLETE)]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg(default_agent=""))
        with mock.patch("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster):
            await d.handle_message(_inbound("/agent beta"))
        assert "Agent set to `beta`" in client.sent[-1][1]
        assert sessions.successes == []  # a command, not a turn
        assert "beta" in d._session_key(_EMAIL)
        await d.handle_message(_inbound("hi"))
        assert sessions.last_agent == "beta"

    @pytest.mark.asyncio
    async def test_bare_agent_lists_only_pickable_agents(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        with mock.patch("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster):
            await d.handle_message(_inbound("/agent"))
        reply = client.sent[-1][1]
        assert "currently `default (kirocrew)`" in reply
        assert "`beta`" in reply
        assert "kirocrew-worker" not in reply

    @pytest.mark.asyncio
    async def test_internal_or_unknown_agent_is_refused(self) -> None:
        sessions = FakeSessions(FakeProvider([AcpEvent(kind=EVENT_COMPLETE)]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg(default_agent=""))
        with mock.patch("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster):
            await d.handle_message(_inbound("/agent kirocrew-worker"))
            await d.handle_message(_inbound("/agent nope"))
        assert all("No agent named" in m for _, m in client.sent)
        await d.handle_message(_inbound("hi"))
        assert sessions.last_agent == "kirocrew"

    def test_agent_is_in_the_help_card(self) -> None:
        assert parse_command("/agent beta") == "agent"
        assert "/agent" in build_help_text()


class TestAgentSwitchGenerations:
    @pytest.mark.asyncio
    async def test_new_after_a_switch_never_resumes_the_new_agents_old_conversation(self) -> None:
        # Restart shape: nothing in memory, beta's bucket already holds gen 1-2.
        from kiro_crew.messaging.link import build_dm_session_key

        sessions = FakeSessions(FakeProvider([]))
        for gen in (1, 2):
            sessions.reserved_generations.add(
                build_dm_session_key("webex", "beta", _EMAIL, gen=gen, dm_scope="per-channel-peer")
            )
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg(default_agent=""))
        with mock.patch("kiro_crew.telegram.transport_dispatch.list_agents", _agent_roster):
            await d.handle_message(_inbound("/agent beta"))
        assert d._session_key(_EMAIL).endswith(":gen2")
        await d.handle_message(_inbound("/new"))
        assert d._session_key(_EMAIL).endswith(":gen3")
