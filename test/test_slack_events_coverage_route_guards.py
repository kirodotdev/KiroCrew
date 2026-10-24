"""Coverage for :mod:`kiro_crew.slack.events`: the ``_route_message`` guard branches.

Activation=off passthrough, channels-governance denial, the display-name fallback
and the other early returns, plus shared messages whose blocks carry malformed
scalars.

Conventions follow ``test_slack_events_coverage.py``: a ``MagicMock`` stand-in for
``GatewayOrchestrator``, ``AsyncMock`` for the Slack client, and
``kiro_crew.slack.events.sel`` patched by the autouse fixtures from
``slack_events_coverage_helpers.py`` so no audit file is written.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _drain, _event, _make_orch

from kiro_crew.config.loader import ACTIVATION_OFF, ACTIVATION_REVIEW, ChannelConfig, SlackConfig
from kiro_crew.slack import events as ev

# ---------------------------------------------------------------------------
# _route_message guard branches
# ---------------------------------------------------------------------------


def _rich_text_scalar_block(element: dict) -> dict:
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [element]}],
    }


_MALFORMED_SHARED_BLOCKS = [
    ("rich-text-text", _rich_text_scalar_block({"type": "text", "text": ["bad"]})),
    (
        "rich-text-link",
        _rich_text_scalar_block({"type": "link", "text": ["bad"], "url": {"bad": "value"}}),
    ),
    (
        "rich-text-emoji",
        _rich_text_scalar_block({"type": "emoji", "name": {"bad": "value"}, "unicode": ["bad"]}),
    ),
    ("rich-text-user", _rich_text_scalar_block({"type": "user", "user_id": ["bad"]})),
    (
        "rich-text-usergroup",
        _rich_text_scalar_block({"type": "usergroup", "usergroup_id": {"bad": "value"}}),
    ),
    (
        "rich-text-channel",
        _rich_text_scalar_block({"type": "channel", "channel_id": ["bad"]}),
    ),
    (
        "rich-text-broadcast",
        _rich_text_scalar_block({"type": "broadcast", "range": {"bad": "value"}}),
    ),
    (
        "rich-text-date",
        _rich_text_scalar_block({"type": "date", "fallback": ["bad"]}),
    ),
    (
        "rich-text-unknown",
        _rich_text_scalar_block({"type": ["attacker-controlled"], "text": {"bad": "value"}}),
    ),
    ("section-text", {"type": "section", "text": {"text": ["bad"]}}),
    ("context-text", {"type": "context", "elements": [{"text": {"bad": "value"}}]}),
]

_MALFORMED_SHARED_ROUTE_CASES = [
    ("top-text-list", {"text": ["bad"]}),
    ("top-text-dict", {"text": {"bad": "value"}}),
    ("event-blocks-non-list", {"blocks": 42}),
    (
        "shared-text-non-string",
        {
            "attachments": [
                {"is_share": True, "text": ["bad"]},
                {"fallback": "plain fallback"},
            ]
        },
    ),
    (
        "shared-fallback-list",
        {
            "attachments": [
                {"is_share": True, "fallback": ["bad"]},
                {"fallback": "plain fallback"},
            ]
        },
    ),
    (
        "shared-fallback-dict",
        {
            "attachments": [
                {"is_share": True, "fallback": {"bad": "value"}},
                {"fallback": "plain fallback"},
            ]
        },
    ),
] + [
    (
        case_name,
        {
            "attachments": [
                {"is_share": True, "blocks": [block]},
                {"fallback": "plain fallback"},
            ]
        },
    )
    for case_name, block in _MALFORMED_SHARED_BLOCKS
]


class TestMalformedSharedMessageScalars:
    def test_extract_blocks_ignores_malformed_scalar_values(self):
        assert (
            ev._extract_blocks_text([block for _case_name, block in _MALFORMED_SHARED_BLOCKS]) == ""
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_fields",
        [
            pytest.param(event_fields, id=case_name)
            for case_name, event_fields in _MALFORMED_SHARED_ROUTE_CASES
        ],
    )
    async def test_route_ignores_malformed_sibling_and_dispatches_plain_fallback(
        self, event_fields, monkeypatch
    ):
        orch = _make_orch()
        monkeypatch.setattr(ev, "_trusted_bot_turns", ev.TrustedBotTurnLedger())
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=[{"fallback": "plain fallback"}],
        )
        event.update(event_fields)

        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)

        hm.assert_awaited_once()
        assert hm.await_args.args[3] == "plain fallback"


class TestRouteMessageGuards:
    @pytest.mark.asyncio
    async def test_activation_off_drops_plain_message(self, _mock_sel):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OFF)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(channel="C1"), ev.SeenCache())
        hm.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "activation=off"

    @pytest.mark.asyncio
    async def test_activation_off_lets_bang_channel_through(self):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OFF)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch, _event(channel="C1", text="!channel on"), ev.SeenCache()
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args.args[3] == "!channel on"

    @pytest.mark.asyncio
    async def test_activation_off_strips_mention_before_bang_check(self):
        orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_OFF)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    _event(channel="C1", text="<@BOT1> !channel on"),
                    ev.SeenCache(),
                    is_mention=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args.args[3] == "!channel on"

    @pytest.mark.asyncio
    async def test_missing_sender_or_text_returns_early(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
            await ev._route_message(orch, _event(user=""), ev.SeenCache())
            await ev._route_message(orch, _event(channel=""), ev.SeenCache())
            await ev._route_message(orch, _event(text="", files=[]), ev.SeenCache())
        hm.assert_not_called()
        orch.channel_history.push.assert_not_called()
        orch.slack.post_message.assert_not_called()
        orch.slack.post_ephemeral.assert_not_called()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_channels_governance_denial_blocks_message(self, _mock_sel):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch(
                "kiro_crew.slack.events.channel_inbound_permitted",
                new_callable=AsyncMock,
                return_value=False,
            ):
                with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                    await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert _mock_sel.log_api_access.call_args.kwargs["error"] == "channels governance policy"

    @pytest.mark.asyncio
    async def test_stop_is_recorded_before_the_liveness_checks(self):
        """A turn between its abandoned attempt and its compaction replay has
        no session and, when it started from an interaction, no registered
        task either -- so this handler would answer "Nothing running." and
        call nothing. The Stop is recorded on the manager FIRST, so the
        replay reads it and stays dropped."""
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=False)
        orch.sessions.get_session_for_thread = MagicMock(return_value=None)
        orch.sessions.note_stop = MagicMock(return_value=True)
        orch.sessions.stop_turn = AsyncMock()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.sessions.note_stop.assert_called_once_with("100.0")
        orch.sessions.stop_turn.assert_not_awaited()
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")

    @pytest.mark.asyncio
    async def test_stop_is_recorded_against_the_threads_owning_session(self):
        """A linked thread's turns -- and their compaction replay -- run under
        the dashboard session that owns the thread, so the Stop must be recorded
        under that key, not the bare thread ts the replay never reads."""
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=False)
        orch.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-7")
        orch.sessions.note_stop = MagicMock(return_value=True)
        orch.sessions.stop_turn = AsyncMock()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        orch.sessions.get_session_for_thread.assert_called_with("100.0")
        orch.sessions.note_stop.assert_called_once_with("dashboard:chat-7")

    @pytest.mark.asyncio
    async def test_flat_dm_stop_targets_a_dashboard_linked_thread_owner(self):
        """With dm_single_session on, `!stop` typed inside a DM thread that a
        dashboard send-to-Slack OWNS must stop that linked owner -- the running
        turn lives under it, keyed by the thread ts -- not the channel-scoped
        flat key. Stopping the flat key would leave the linked turn's provider
        running while acking a session that was never busy. A self-derived owner
        (``slack:<thread_ts>``) is not a real binding and does not win."""
        orch = _make_orch(use_transport=True)
        orch._cfg.slack = SlackConfig(dm_single_session=True)
        orch.sessions.has_session = MagicMock(return_value=True)
        # The thread ts (90.0) is owned by a dashboard session; the flat key
        # (slack:D1) is not what the turn runs under.
        orch.sessions.get_session_for_thread = MagicMock(
            side_effect=lambda k: "dashboard:chat-7" if k == "90.0" else None
        )
        orch.sessions.note_stop = MagicMock(return_value=True)
        orch.sessions.stop_turn = AsyncMock(return_value="stopped")
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(
                    orch, _event(text="!stop", thread_ts="90.0", ts="100.0"), ev.SeenCache()
                )
        # The linked owner is what gets stopped and cleared -- never the flat key.
        orch.sessions.stop_turn.assert_awaited_once()
        assert orch.sessions.stop_turn.await_args.args[0] == "dashboard:chat-7"
        orch.sessions.note_stop.assert_called_once_with("dashboard:chat-7")

    @pytest.mark.asyncio
    async def test_flat_dm_stop_ignores_a_self_derived_thread_owner(self):
        """A DM thread claimed only by its own per-thread session
        (``slack:<thread_ts>`` -- the shape dm_single_session merges away) is
        NOT a real binding, so `!stop` still targets the flat channel key."""
        orch = _make_orch(use_transport=True)
        orch._cfg.slack = SlackConfig(dm_single_session=True)
        orch.sessions.has_session = MagicMock(return_value=True)
        # Only a self-derived owner exists: slack:<thread_ts>.
        orch.sessions.get_session_for_thread = MagicMock(
            side_effect=lambda k: "slack:90.0" if k == "90.0" else None
        )
        orch.sessions.note_stop = MagicMock(return_value=True)
        orch.sessions.stop_turn = AsyncMock(return_value="stopped")
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                await ev._route_message(
                    orch, _event(text="!stop", thread_ts="90.0", ts="100.0"), ev.SeenCache()
                )
        orch.sessions.stop_turn.assert_awaited_once()
        assert orch.sessions.stop_turn.await_args.args[0] == "slack:D1"

    @pytest.mark.asyncio
    async def test_pure_stop_is_exempt_from_governance_denial(self):
        orch = _make_orch()
        orch.sessions.has_session = MagicMock(return_value=False)
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.is_owner", return_value=True):
                with patch(
                    "kiro_crew.slack.events.channel_inbound_permitted",
                    new_callable=AsyncMock,
                    return_value=False,
                ) as gate:
                    await ev._route_message(orch, _event(text="!stop"), ev.SeenCache())
        gate.assert_not_called()
        orch.slack.post_message.assert_awaited_with("D1", "Nothing running.", "100.0")

    @pytest.mark.asyncio
    async def test_display_name_resolved_from_slack(self):
        orch = _make_orch()
        orch.slack.get_user_info = AsyncMock(return_value={"real_name": "Ada L"})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
                await _drain(orch)
        assert hm.await_args.kwargs["user_display_name"] == "Ada L"
        orch.channel_history.set_user_name.assert_called_with("U_OWNER", "Ada L")

    @pytest.mark.asyncio
    async def test_display_name_lookup_failure_falls_back_to_config(self):
        orch = _make_orch()
        orch._cfg.slack.allowed_users = [{"slack_id": "U_OWNER", "name": "Ada From Config"}]
        orch.slack.get_user_info = AsyncMock(side_effect=RuntimeError("no scope"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
                await _drain(orch)
        assert hm.await_args.kwargs["user_display_name"] == "Ada From Config"

    @pytest.mark.asyncio
    async def test_unauthorized_sender_gets_ephemeral(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        hm.assert_not_called()
        assert "not authorized" in orch.slack.post_ephemeral.await_args[0][2]

    @pytest.mark.asyncio
    async def test_trusted_bot_is_authorized_and_processed(self, _mock_sel):
        """from_trusted_bot=True: the bot_id stands in as sender and the message is processed."""
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    _event(user="", bot_id="B_TRUSTED"),
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args[0][6] == "B_TRUSTED"  # sender_id positional
        assert hm.await_args.kwargs["from_trusted_bot"] is True
        orch.slack.post_ephemeral.assert_not_awaited()
        # The admission is a permission decision: audited with its basis.
        allowed = [
            c.kwargs
            for c in _mock_sel.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "allowed"
        ]
        assert allowed and allowed[0]["caller"] == "B_TRUSTED"
        assert allowed[0]["resources"] == "trusted_bot"

    @pytest.mark.asyncio
    async def test_trusted_bot_attachment_fallback_is_recovered(self):
        orch = _make_orch()
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=[{"fallback": "🚨 CloudWatch Alarm | myapi-5xx-alarm"}],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        assert hm.await_args is not None, "trusted-bot attachment text was dropped"
        assert hm.await_args[0][3] == "🚨 CloudWatch Alarm | myapi-5xx-alarm"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "attachments",
        [
            pytest.param("bad", id="non-list"),
            pytest.param([None], id="non-dict-member"),
        ],
    )
    async def test_trusted_bot_malformed_attachments_do_not_dispatch(self, attachments: object):
        orch = _make_orch()
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=attachments,
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_not_awaited()
        orch.channel_history.push.assert_not_called()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_trusted_bot_malformed_attachment_member_is_skipped(self):
        orch = _make_orch()
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            ts="101.0",
            attachments=[None, {"fallback": "plain fallback"}],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args is not None
        assert hm.await_args.args[3] == "plain fallback"

    @pytest.mark.asyncio
    async def test_trusted_bot_attachment_blocks_are_recovered(self):
        orch = _make_orch()
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=[
                {
                    "fallback": "This message contains interactive elements.",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "Alarm is *firing*"},
                        }
                    ],
                }
            ],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        assert hm.await_args is not None, "trusted-bot attachment blocks were dropped"
        assert hm.await_args[0][3] == "Alarm is *firing*"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", ["is_share", "is_msg_unfurl"])
    async def test_trusted_bot_field_only_shared_attachment_is_not_reclassified(self, marker):
        orch = _make_orch()
        attachment = {
            marker: True,
            "fallback": "This message contains interactive elements.",
            "blocks": [
                {
                    "type": "section",
                    "fields": [{"type": "mrkdwn", "text": "shared field"}],
                }
            ],
        }
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=[attachment],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_not_awaited()
        orch.channel_history.push.assert_not_called()
        assert orch._session_tasks == {}

    @pytest.mark.parametrize("marker", ["is_share", "is_msg_unfurl"])
    def test_trusted_bot_shared_attachment_is_skipped_before_content(self, marker):
        class _UnreadContentAttachment(dict):
            def get(self, key, default=None):
                if key in {"fallback", "blocks"}:
                    raise AssertionError(f"{key} read after shared classification")
                return super().get(key, default)

        attachment = _UnreadContentAttachment(
            {
                marker: True,
                "fallback": "preview fallback",
                "blocks": [
                    {
                        "type": "section",
                        "fields": [{"type": "mrkdwn", "text": "preview field"}],
                    }
                ],
            }
        )
        assert ev._extract_trusted_bot_attachment_text({"attachments": [attachment]}) == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fallback", ["", "This message contains interactive elements."])
    async def test_trusted_bot_section_fields_are_recovered(self, fallback):
        orch = _make_orch()
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=[
                {
                    "fallback": fallback,
                    "blocks": [
                        {
                            "type": "section",
                            "fields": [
                                {"type": "mrkdwn", "text": "Alarm: *firing*"},
                                {"type": "mrkdwn", "text": "Region: `us-west-2`"},
                            ],
                        }
                    ],
                }
            ],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args.args[3] == "Alarm: *firing*\nRegion: `us-west-2`"

    def test_trusted_bot_section_fields_skip_malformed_and_stop_at_budget(self):
        class _ExplodingField(dict):
            def get(self, key, default=None):
                raise AssertionError("field inspected after the renderer budget was spent")

        prefix = "x" * (ev._MAX_RECOVERED_TEXT_CHARS - 5)
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {"fallback": prefix},
                    {
                        "fallback": "This message contains interactive elements.",
                        "blocks": [
                            {
                                "type": "section",
                                "fields": [
                                    None,
                                    {"type": "mrkdwn", "text": 42},
                                    {
                                        "type": "mrkdwn",
                                        "text": "abc" + "z" * ev._MAX_RECOVERED_TEXT_CHARS,
                                    },
                                    _ExplodingField(),
                                ],
                            }
                        ],
                    },
                ]
            }
        )
        assert recovered == prefix + "\n\nabc"
        assert len(recovered) == ev._MAX_RECOVERED_TEXT_CHARS

    def test_trusted_bot_padded_generic_fallback_uses_blocks(self):
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {
                        "fallback": "  This message contains interactive elements.  ",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": "real"},
                            }
                        ],
                    }
                ]
            }
        )
        assert recovered == "real"

    def test_trusted_bot_attachment_real_fallback_precedes_blocks(self):
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {
                        "fallback": "plain fallback",
                        "blocks": [
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": "block text"},
                            }
                        ],
                    }
                ]
            }
        )
        assert recovered == "plain fallback"

    @pytest.mark.asyncio
    async def test_human_attachment_fallback_stays_excluded(self):
        orch = _make_orch()
        event = _event(text="", attachments=[{"fallback": "link preview"}])
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, event, ev.SeenCache())
        hm.assert_not_called()
        orch.channel_history.push.assert_not_called()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_trusted_bot_attachment_blocks_dispatch_the_shared_rendering(self):
        """Attachment Block Kit is rendered by the one renderer the shared path uses."""
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "elements": [
                            {
                                "elements": [
                                    {"type": "text", "text": "one"},
                                    {"type": "emoji", "name": "tada"},
                                ]
                            }
                        ],
                    },
                    {
                        "type": "rich_text_quote",
                        "elements": [{"type": "link", "text": "site", "url": "https://a.invalid"}],
                    },
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": "section"}},
            {"type": "context", "elements": [{"text": "context"}]},
        ]
        orch = _make_orch()
        event = _event(
            user="",
            bot_id="B_TRUSTED",
            text="",
            attachments=[
                {"fallback": "This message contains interactive elements.", "blocks": blocks}
            ],
        )
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    event,
                    ev.SeenCache(),
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args[0][3] == "- one:tada:\n> site (https://a.invalid)\nsection\ncontext"
        assert hm.await_args[0][3] == ev._extract_blocks_text(blocks)

    def test_trusted_bot_attachment_recovery_validates_and_caps_input(self):
        assert ev._extract_trusted_bot_attachment_text({"attachments": "bad"}) == ""
        assert (
            ev._extract_trusted_bot_attachment_text(
                {"attachments": [None, {"fallback": 42, "blocks": "bad"}]}
            )
            == ""
        )
        recovered = ev._extract_trusted_bot_attachment_text(
            {"attachments": [{"fallback": "x" * (ev._MAX_RECOVERED_TEXT_CHARS + 1)}]}
        )
        assert len(recovered) == ev._MAX_RECOVERED_TEXT_CHARS

    def test_trusted_bot_whitespace_only_fallback_is_skipped(self):
        assert (
            ev._extract_trusted_bot_attachment_text({"attachments": [{"fallback": " \n "}]}) == ""
        )
        recovered = ev._extract_trusted_bot_attachment_text(
            {"attachments": [{"fallback": "a"}, {"fallback": "  "}, {"fallback": " b "}]}
        )
        assert recovered == "a\n\nb"

    def test_trusted_bot_attachment_cap_stops_before_later_attachment(self):
        class _ExplodingAttachment(dict):
            def get(self, key, default=None):
                raise AssertionError("attachment inspected after recovery cap")

        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {"fallback": "x" * ev._MAX_RECOVERED_TEXT_CHARS},
                    _ExplodingAttachment(),
                ]
            }
        )
        assert recovered == "x" * ev._MAX_RECOVERED_TEXT_CHARS

    def test_trusted_bot_attachment_fallback_gets_only_the_remaining_budget(self):
        prefix = "a" * (ev._MAX_RECOVERED_TEXT_CHARS - 3)
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {"fallback": prefix},
                    {"fallback": "bc" + "x" * ev._MAX_RECOVERED_TEXT_CHARS},
                ]
            }
        )
        assert recovered == prefix + "\n\nb"
        assert len(recovered) == ev._MAX_RECOVERED_TEXT_CHARS

    def test_trusted_bot_attachment_blocks_get_only_the_remaining_budget(self):
        class _ExplodingElement(dict):
            def get(self, key, default=None):
                raise AssertionError("inline element inspected after recovery cap")

        prefix = "a" * (ev._MAX_RECOVERED_TEXT_CHARS - 3)
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {"fallback": prefix},
                    {
                        "fallback": "This message contains interactive elements.",
                        "blocks": [
                            {
                                "type": "rich_text",
                                "elements": [
                                    {
                                        "type": "rich_text_section",
                                        "elements": [
                                            {
                                                "type": "text",
                                                "text": "bc" + "x" * ev._MAX_RECOVERED_TEXT_CHARS,
                                            },
                                            _ExplodingElement(),
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        )
        assert recovered == prefix + "\n\nb"
        assert len(recovered) == ev._MAX_RECOVERED_TEXT_CHARS

    def test_trusted_bot_renderer_exhaustion_does_not_admit_later_attachment(self):
        prefix = "x" * (ev._MAX_RECOVERED_TEXT_CHARS - 10)
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "A"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "      "}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "B"}},
        ]
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {"fallback": prefix},
                    {
                        "fallback": "This message contains interactive elements.",
                        "blocks": blocks,
                    },
                    {"fallback": "C"},
                ]
            }
        )
        assert recovered == prefix + "\n\nA"

    def test_trusted_bot_renderer_consumption_includes_stripped_whitespace(self):
        prefix = "x" * (ev._MAX_RECOVERED_TEXT_CHARS - 12)
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "A"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "      "}},
        ]
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {"fallback": prefix},
                    {
                        "fallback": "This message contains interactive elements.",
                        "blocks": blocks,
                    },
                    {"fallback": "C"},
                ]
            }
        )
        assert recovered == prefix + "\n\nA"

    @pytest.mark.parametrize(
        "prefix_size", [ev._MAX_RECOVERED_TEXT_CHARS - 2, ev._MAX_RECOVERED_TEXT_CHARS - 1]
    )
    def test_trusted_bot_attachment_without_room_for_a_separator_is_skipped(self, prefix_size):
        class _ExplodingAttachment(dict):
            def get(self, key, default=None):
                raise AssertionError("attachment inspected with no room left for it")

        prefix = "a" * prefix_size
        recovered = ev._extract_trusted_bot_attachment_text(
            {"attachments": [{"fallback": prefix}, _ExplodingAttachment()]}
        )
        assert recovered == prefix

    def test_trusted_bot_blocks_ignore_non_string_nested_scalars(self):
        recovered = ev._extract_trusted_bot_attachment_text(
            {
                "attachments": [
                    {
                        "fallback": "This message contains interactive elements.",
                        "blocks": [
                            {
                                "type": "rich_text",
                                "elements": [
                                    {
                                        "type": "rich_text_section",
                                        "elements": [
                                            {"type": "text", "text": {"bad": "value"}},
                                            {"type": "link", "text": ["bad"], "url": 17},
                                            {
                                                "type": "emoji",
                                                "name": {"bad": "value"},
                                                "unicode": 9,
                                            },
                                            {"type": "date", "fallback": ["bad"]},
                                            {"type": "mystery", "text": None},
                                            {"type": ["unhashable"], "text": "unknown "},
                                            {"type": "text", "text": "safe"},
                                        ],
                                    }
                                ],
                            },
                            {"type": "section", "text": {"text": 42}},
                            {
                                "type": "context",
                                "elements": [{"text": ["bad"]}, {"text": "context"}],
                            },
                        ],
                    }
                ]
            }
        )
        assert recovered == "unknown safe\ncontext"

    @pytest.mark.asyncio
    async def test_trusted_bot_turn_limit_caps_the_thread(self, _mock_sel):
        """The (limit+1)-th consecutive trusted-bot turn in one thread is denied."""
        orch = _make_orch()
        orch._cfg.slack.trusted_bot_turn_limit = 2
        seen = ev.SeenCache()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                for i in range(3):
                    await ev._route_message(
                        orch,
                        _event(
                            user="",
                            bot_id="B_TRUSTED",
                            channel="D_CAP",
                            ts=f"200.{i}",
                            thread_ts="200.0",
                        ),
                        seen,
                        from_trusted_bot=True,
                    )
                    await _drain(orch)
        assert hm.await_count == 2  # third turn denied by the cap
        denied = [
            c.kwargs
            for c in _mock_sel.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert denied and denied[-1]["error"] == "trusted_bot_turn_limit_reached"

    @pytest.mark.asyncio
    async def test_human_message_resets_the_turn_count(self):
        """An allowed human's message re-opens a capped thread."""
        orch = _make_orch()
        orch._cfg.slack.trusted_bot_turn_limit = 1
        seen = ev.SeenCache()
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
                # Turn 1 admitted, turn 2 capped (limit=1).
                for i in range(2):
                    await ev._route_message(
                        orch,
                        _event(
                            user="",
                            bot_id="B_TRUSTED",
                            channel="D_RESET",
                            ts=f"300.{i}",
                            thread_ts="300.0",
                        ),
                        seen,
                        from_trusted_bot=True,
                    )
                    await _drain(orch)
            assert hm.await_count == 1
            # Allowed human posts in the thread: count resets.
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await ev._route_message(
                    orch,
                    _event(user="U_OWNER", channel="D_RESET", ts="300.5", thread_ts="300.0"),
                    seen,
                )
                await _drain(orch)
            # Trusted bot admitted again after the reset.
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
                await ev._route_message(
                    orch,
                    _event(
                        user="",
                        bot_id="B_TRUSTED",
                        channel="D_RESET",
                        ts="300.9",
                        thread_ts="300.0",
                    ),
                    seen,
                    from_trusted_bot=True,
                )
                await _drain(orch)
        assert hm.await_count == 3  # bot turn 1 + human turn + bot turn after reset

    @pytest.mark.asyncio
    async def test_turn_counts_are_per_thread(self):
        """A capped thread does not cap a different thread."""
        orch = _make_orch()
        orch._cfg.slack.trusted_bot_turn_limit = 1
        seen = ev.SeenCache()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                # Cap thread A (limit=1: first turn admitted, second denied).
                for i in range(2):
                    await ev._route_message(
                        orch,
                        _event(
                            user="",
                            bot_id="B_TRUSTED",
                            channel="D_ISO",
                            ts=f"400.{i}",
                            thread_ts="400.0",
                        ),
                        seen,
                        from_trusted_bot=True,
                    )
                    await _drain(orch)
                # Thread B is unaffected.
                await ev._route_message(
                    orch,
                    _event(
                        user="",
                        bot_id="B_TRUSTED",
                        channel="D_ISO",
                        ts="500.1",
                        thread_ts="500.0",
                    ),
                    seen,
                    from_trusted_bot=True,
                )
                await _drain(orch)
        assert hm.await_count == 2  # thread A turn 1 + thread B turn 1

    @pytest.mark.asyncio
    async def test_non_dispatching_messages_do_not_burn_the_budget(self):
        """An empty-after-strip mention does not move the turn count (round-5 pin)."""
        orch = _make_orch()
        orch._cfg.slack.trusted_bot_turn_limit = 1
        seen = ev.SeenCache()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                # A bare-mention message strips to empty clean_text: it returns
                # before dispatch and must NOT consume the thread's only turn.
                await ev._route_message(
                    orch,
                    _event(
                        user="",
                        bot_id="B_TRUSTED",
                        channel="D_EMPTY",
                        text="<@U_ME>",
                        ts="600.1",
                        thread_ts="600.0",
                    ),
                    seen,
                    is_mention=True,
                    from_trusted_bot=True,
                )
                await _drain(orch)
                hm.assert_not_called()
                # The thread's budget is intact: a real message still dispatches.
                await ev._route_message(
                    orch,
                    _event(
                        user="",
                        bot_id="B_TRUSTED",
                        channel="D_EMPTY",
                        text="real content",
                        ts="600.2",
                        thread_ts="600.0",
                    ),
                    seen,
                    from_trusted_bot=True,
                )
                await _drain(orch)
        hm.assert_awaited_once()
        assert hm.await_args.args[3] == "real content"

    @pytest.mark.asyncio
    async def test_bot_without_trusted_flag_is_not_processed(self):
        """Fail-closed pin: a bot event routed WITHOUT the flag has no sender and is dropped."""
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    _event(user="", bot_id="B_ANY"),
                    ev.SeenCache(),
                    from_trusted_bot=False,
                )
        hm.assert_not_called()
        orch.channel_history.push.assert_not_called()
        orch.slack.post_ephemeral.assert_not_called()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_trusted_bot_denied_in_review_channel(self, _mock_sel):
        """Review mode denies trusted bots: the draft flow's ephemeral needs a human user id."""
        orch = _make_orch(channels={"C_REV": ChannelConfig(activation=ACTIVATION_REVIEW)})
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch,
                    _event(user="", bot_id="B_TRUSTED", channel="C_REV", is_mention=True),
                    ev.SeenCache(),
                    is_mention=True,
                    from_trusted_bot=True,
                )
        hm.assert_not_called()
        denied = [
            c.kwargs
            for c in _mock_sel.log_api_access.call_args_list
            if c.kwargs.get("outcome") == "denied"
        ]
        assert denied and denied[0]["error"] == "trusted_bot_denied_in_review_channel"

    @pytest.mark.asyncio
    async def test_ephemeral_rejection_failure_is_swallowed(self):
        orch = _make_orch()
        orch.slack.post_ephemeral = AsyncMock(side_effect=RuntimeError("channel_not_found"))
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), ev.SeenCache())
        orch.slack.post_ephemeral.assert_awaited_once()
        assert orch.slack.post_ephemeral.await_args.args[:2] == ("D1", "U_OWNER")
        assert "not authorized" in orch.slack.post_ephemeral.await_args.args[2]
        hm.assert_not_called()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_duplicate_event_is_dropped(self):
        orch = _make_orch()
        seen = ev.SeenCache()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(orch, _event(), seen)
                await _drain(orch)
                await ev._route_message(orch, _event(), seen)
                await _drain(orch)
        assert hm.await_count == 1

    @pytest.mark.asyncio
    async def test_missing_channel_history_is_reported_not_fatal(self, caplog):
        orch = _make_orch()
        orch.channel_history = None
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                with caplog.at_level("ERROR", logger="kiro_crew.slack.events"):
                    await ev._route_message(orch, _event(), ev.SeenCache())
                    await _drain(orch)
        hm.assert_awaited_once()
        assert any("channel_history not initialised" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_mention_only_text_is_dropped(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as hm:
                await ev._route_message(
                    orch, _event(text="<@BOT1>"), ev.SeenCache(), is_mention=True
                )
                await _drain(orch)
        hm.assert_not_called()
        # The mention-only text still reaches the history buffer; only dispatch is skipped.
        orch.channel_history.push.assert_called_once()
        assert orch._session_tasks == {}

    @pytest.mark.asyncio
    async def test_handle_message_task_creation_failure_is_contained(self):
        orch = _make_orch()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            # `new=` (not side_effect on an autospecced AsyncMock) so the *call*
            # raises, exercising the create_task guard rather than the task body.
            with patch(
                "kiro_crew.slack.events.handle_message",
                new=MagicMock(side_effect=RuntimeError("cannot build coroutine")),
            ):
                await ev._route_message(orch, _event(), ev.SeenCache())
        assert orch._session_tasks == {}
