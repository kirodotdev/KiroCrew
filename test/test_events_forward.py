"""Tests for forwarded-message attachment text recovery in slack.events."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import (
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_REVIEW,
    ChannelConfig,
)
from kiro_crew.slack.events import (
    _MAX_RECOVERED_TEXT_CHARS,
    _SLACK_BLOCK_FALLBACKS,
    _extract_blocks_text,
    _extract_shared_text,
    _normalize_message_blocks,
    _route_message,
)


class TestRenderRichTextElements:
    """Unit tests for element-type coverage in _extract_blocks_text."""

    def _blocks_with_elements(self, elements: list[dict]) -> list[dict]:
        return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}]

    def test_text_element(self):
        blocks = self._blocks_with_elements([{"type": "text", "text": "hello"}])
        assert _extract_blocks_text(blocks) == "hello"

    def test_link_with_text_and_url(self):
        blocks = self._blocks_with_elements([
            {"type": "link", "url": "https://example.com", "text": "Example"}
        ])
        assert _extract_blocks_text(blocks) == "Example (https://example.com)"

    def test_link_url_only(self):
        blocks = self._blocks_with_elements([{"type": "link", "url": "https://example.com"}])
        assert _extract_blocks_text(blocks) == "https://example.com"

    def test_link_text_only(self):
        blocks = self._blocks_with_elements([{"type": "link", "text": "click here"}])
        assert _extract_blocks_text(blocks) == "click here"

    def test_emoji_with_name(self):
        blocks = self._blocks_with_elements([{"type": "emoji", "name": "thumbsup"}])
        assert _extract_blocks_text(blocks) == ":thumbsup:"

    def test_emoji_unicode_fallback(self):
        blocks = self._blocks_with_elements([{"type": "emoji", "unicode": "\U0001f44d"}])
        assert _extract_blocks_text(blocks) == "\U0001f44d"

    def test_user_mention(self):
        blocks = self._blocks_with_elements([{"type": "user", "user_id": "U12345"}])
        assert _extract_blocks_text(blocks) == "<@U12345>"

    def test_usergroup_mention(self):
        blocks = self._blocks_with_elements([{"type": "usergroup", "usergroup_id": "S041"}])
        assert _extract_blocks_text(blocks) == "<!subteam^S041>"

    def test_channel_mention(self):
        blocks = self._blocks_with_elements([{"type": "channel", "channel_id": "C999"}])
        assert _extract_blocks_text(blocks) == "<#C999>"

    def test_broadcast(self):
        blocks = self._blocks_with_elements([{"type": "broadcast", "range": "here"}])
        assert _extract_blocks_text(blocks) == "<!here>"

    def test_date_with_fallback(self):
        blocks = self._blocks_with_elements([
            {"type": "date", "timestamp": 1234567890, "format": "{date}", "fallback": "Jan 1, 2025"}
        ])
        assert _extract_blocks_text(blocks) == "Jan 1, 2025"

    def test_date_without_fallback(self):
        blocks = self._blocks_with_elements([{"type": "date", "timestamp": 1234567890, "format": "{date}"}])
        assert _extract_blocks_text(blocks) == ""


class TestExtractBlocksText:
    def test_rich_text_section(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "text", "text": "hello world"}],
                    }
                ],
            }
        ]
        assert _extract_blocks_text(blocks) == "hello world"

    def test_rich_text_preformatted(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_preformatted",
                        "elements": [{"type": "text", "text": "code block content"}],
                    }
                ],
            }
        ]
        assert _extract_blocks_text(blocks) == "code block content"

    def test_rich_text_list_with_bullet_markers(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_list",
                        "elements": [
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": "item 1"}]},
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": "item 2"}]},
                        ],
                    }
                ],
            }
        ]
        result = _extract_blocks_text(blocks)
        assert result == "- item 1\n- item 2"

    def test_rich_text_quote_with_prefix(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_quote",
                        "elements": [{"type": "text", "text": "quoted text"}],
                    }
                ],
            }
        ]
        assert _extract_blocks_text(blocks) == "> quoted text"

    def test_section_block(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "section text"}}]
        assert _extract_blocks_text(blocks) == "section text"

    def test_link_elements(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "see "},
                            {"type": "link", "url": "https://example.com"},
                        ],
                    }
                ],
            }
        ]
        assert _extract_blocks_text(blocks) == "see https://example.com"

    def test_empty_blocks(self):
        assert _extract_blocks_text([]) == ""

    def test_actions_blocks_ignored(self):
        blocks = [
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Click"}}]}
        ]
        assert _extract_blocks_text(blocks) == ""

    def test_whitespace_only_returns_empty(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": "   \n  "}]}
                ],
            }
        ]
        assert _extract_blocks_text(blocks) == ""

    def test_truncation_at_max_chars(self):
        long_text = "x" * (_MAX_RECOVERED_TEXT_CHARS + 1000)
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "text", "text": long_text}]}
                ],
            }
        ]
        result = _extract_blocks_text(blocks)
        assert len(result) == _MAX_RECOVERED_TEXT_CHARS


class TestExtractBlocksTextDefensive:
    """Adversarial/negative tests: malformed input must not raise."""

    def test_non_dict_block_in_list(self):
        blocks = ["not a dict", 42, None, {"type": "section", "text": {"type": "mrkdwn", "text": "ok"}}]
        assert _extract_blocks_text(blocks) == "ok"  # type: ignore[arg-type]

    def test_non_list_elements(self):
        blocks = [{"type": "rich_text", "elements": "not a list"}]
        assert _extract_blocks_text(blocks) == ""

    def test_non_dict_element_in_elements(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {"type": "rich_text_section", "elements": [42, "bad", {"type": "text", "text": "ok"}]}
                ],
            }
        ]
        assert _extract_blocks_text(blocks) == "ok"

    def test_section_with_non_dict_text(self):
        blocks = [{"type": "section", "text": "bare string"}]
        assert _extract_blocks_text(blocks) == ""

    def test_section_with_none_text(self):
        blocks = [{"type": "section", "text": None}]
        assert _extract_blocks_text(blocks) == ""

    def test_context_with_non_dict_element(self):
        blocks = [{"type": "context", "elements": [None, 123, {"type": "mrkdwn", "text": "ctx"}]}]
        assert _extract_blocks_text(blocks) == "ctx"

    def test_context_with_none_elements(self):
        blocks = [{"type": "context", "elements": None}]
        assert _extract_blocks_text(blocks) == ""

    def test_rich_text_with_none_elements(self):
        blocks = [{"type": "rich_text", "elements": None}]
        assert _extract_blocks_text(blocks) == ""

    def test_rich_text_section_with_none_elements(self):
        blocks = [
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_section", "elements": None}],
            }
        ]
        assert _extract_blocks_text(blocks) == ""


class TestNormalizeMessageBlocks:
    """Test the message_blocks wrapper-shape normalization."""

    def test_wrapper_shape(self):
        raw = [
            {
                "team": "T123",
                "channel": "C456",
                "ts": "1234.5678",
                "message": {
                    "blocks": [
                        {"type": "rich_text", "elements": [
                            {"type": "rich_text_section", "elements": [{"type": "text", "text": "inner"}]}
                        ]}
                    ]
                },
            }
        ]
        result = _normalize_message_blocks(raw)
        assert len(result) == 1
        assert result[0]["type"] == "rich_text"

    def test_non_list_input(self):
        assert _normalize_message_blocks("not a list") == []  # type: ignore[arg-type]

    def test_non_dict_items(self):
        assert _normalize_message_blocks([42, None, "bad"]) == []

    def test_missing_message_key(self):
        assert _normalize_message_blocks([{"team": "T1"}]) == []


class TestExtractSharedText:
    def test_single_share_attachment_returns_text(self):
        event = {"text": "", "attachments": [{"is_share": True, "text": "forwarded body"}]}
        assert _extract_shared_text(event) == "forwarded body"

    def test_is_msg_unfurl_attachment_included(self):
        event = {"attachments": [{"is_msg_unfurl": True, "text": "shared msg"}]}
        assert _extract_shared_text(event) == "shared msg"

    def test_falls_back_to_fallback_field(self):
        event = {"attachments": [{"is_share": True, "fallback": "[10:00] Bob: hi"}]}
        assert _extract_shared_text(event) == "[10:00] Bob: hi"

    def test_multiple_shares_joined(self):
        event = {
            "attachments": [
                {"is_share": True, "text": "first"},
                {"is_share": True, "text": "second"},
            ]
        }
        assert _extract_shared_text(event) == "first\n\nsecond"

    def test_link_unfurl_excluded(self):
        event = {"attachments": [{"title": "Some Page", "text": "preview text"}]}
        assert _extract_shared_text(event) == ""

    def test_no_attachments_returns_empty(self):
        assert _extract_shared_text({"text": ""}) == ""

    def test_empty_share_parts_filtered(self):
        event = {
            "attachments": [
                {"is_share": True, "text": ""},
                {"is_share": True, "text": "kept"},
            ]
        }
        assert _extract_shared_text(event) == "kept"

    def test_interactive_elements_fallback_suppressed(self):
        """When fallback is Slack's generic Block Kit placeholder, don't use it."""
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "fallback": "This message contains interactive elements.",
                }
            ]
        }
        assert _extract_shared_text(event) == ""

    def test_interactive_elements_fallback_uses_blocks(self):
        """Extract content from attachment blocks when fallback is generic."""
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "fallback": "This message contains interactive elements.",
                    "blocks": [
                        {
                            "type": "rich_text",
                            "elements": [
                                {
                                    "type": "rich_text_section",
                                    "elements": [{"type": "text", "text": "actual content from blocks"}],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        assert _extract_shared_text(event) == "actual content from blocks"

    def test_event_level_blocks_fallback(self):
        """When attachments yield nothing, try event-level blocks."""
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "fallback": "This message contains interactive elements.",
                }
            ],
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "event-level content"}],
                        }
                    ],
                }
            ],
        }
        assert _extract_shared_text(event) == "event-level content"

    def test_non_generic_fallback_still_used(self):
        """Custom fallback text (not Slack's placeholder) is still returned."""
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "fallback": "Bob posted: check this out",
                }
            ]
        }
        assert _extract_shared_text(event) == "Bob posted: check this out"

    def test_message_blocks_wrapper_shape_recovered(self):
        """Content from message_blocks wrapper structure is recovered."""
        event = {
            "attachments": [
                {
                    "is_share": True,
                    "text": "",
                    "message_blocks": [
                        {
                            "team": "T123",
                            "channel": "C456",
                            "ts": "1234.5678",
                            "message": {
                                "blocks": [
                                    {
                                        "type": "rich_text",
                                        "elements": [
                                            {
                                                "type": "rich_text_section",
                                                "elements": [{"type": "text", "text": "from message_blocks"}],
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    ],
                }
            ]
        }
        assert _extract_shared_text(event) == "from message_blocks"


class TestRouteMessageFallbackRecovery:
    """Test that _route_message recovers content from blocks when text is a
    generic Slack fallback — tests actually call _route_message with mocks."""

    @pytest.mark.asyncio
    async def test_placeholder_text_with_no_recoverable_blocks_drops_message(self):
        """When blocks have no extractable text and text is placeholder, message is dropped
        by the (not text and not files) guard — _route_message returns without dispatching."""
        from kiro_crew.slack.events import _route_message

        event = {
            "user": "U123",
            "channel": "C456",
            "text": "This message contains interactive elements.",
            "ts": "1234.5678",
            "team": "T789",
            "blocks": [
                {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Btn"}}]}
            ],
        }

        mock_orch = AsyncMock()
        mock_seen = AsyncMock()
        mock_seen.check_and_add = lambda x: False
        mock_seen.check = lambda x: False  # SeenCache.check: unseen
        mock_seen.add = lambda x: None  # SeenCache.add: no-op in test

        with patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True), \
             patch("kiro_crew.slack.events.sel") as mock_sel, \
             patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_sel.return_value.log_api_access = lambda **kw: None
            await _route_message(mock_orch, event, mock_seen, is_mention=False)
            # Message should be dropped — handle_message never called
            mock_handle.assert_not_called()

    @pytest.mark.asyncio
    async def test_placeholder_text_replaced_by_block_content(self):
        """_route_message passes recovered text (not the placeholder) past the early guard."""
        from kiro_crew.slack.events import _route_message

        event = {
            "user": "U123",
            "channel": "C456",
            "text": "This message contains interactive elements.",
            "ts": "1234.5678",
            "team": "T789",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": "Real user content"}],
                        }
                    ],
                }
            ],
        }

        from unittest.mock import MagicMock

        mock_orch = AsyncMock()
        ch_cfg = MagicMock()
        ch_cfg.activation = "mention"
        ch_cfg.thread_follow = True
        mock_cfg = MagicMock()
        mock_cfg.channel_config.return_value = ch_cfg
        mock_orch._cfg = mock_cfg
        mock_orch.channel_history = None
        mock_orch.sessions = None
        mock_orch.conv_log = None
        mock_orch.slack = None
        mock_orch._session_tasks = {}
        mock_seen = MagicMock()
        mock_seen.check_and_add = lambda x: False
        mock_seen.check = lambda x: False  # SeenCache.check: unseen
        mock_seen.add = lambda x: None  # SeenCache.add: no-op in test

        with patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True), \
             patch("kiro_crew.slack.events.sel") as mock_sel, \
             patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.is_owner", return_value=True), \
             patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_sel.return_value.log_api_access = lambda **kw: None
            await _route_message(mock_orch, event, mock_seen, is_mention=True)
            # handle_message SHOULD be called — text was recovered and is non-empty
            assert mock_handle.called
            # The text arg should contain recovered content, not placeholder
            call_args = mock_handle.call_args
            # handle_message is called with keyword args including text=
            all_args_str = str(call_args)
            assert "Real user content" in all_args_str
            assert "This message contains interactive elements." not in all_args_str

    @pytest.mark.asyncio
    async def test_interceptor_redirect_audits_dedups_and_short_circuits(self):
        """A REDIRECTED gate decision must: short-circuit before handle_message,
        emit a SEL audit for the intercept decision, and dedup event retries so a
        redirecting adapter mints only ONE challenge per event."""
        import dataclasses
        from unittest.mock import MagicMock

        from kiro_crew.platform import build_default_context
        from kiro_crew.platform.context import set_context
        from kiro_crew.platform.interfaces import InterceptDecision
        from kiro_crew.slack.events import SeenCache, _route_message

        calls = {"intercept": 0}

        class _RedirectGate:
            # Minimal SlackEnterpriseGate: always REDIRECTED (a challenge issued).
            def validate_enterprise(self, *a, **k):
                return True

            def check_message_origin(self, *a, **k):
                return True

            def heartbeat_safe_tools(self):
                return frozenset()

            def intercept_message(self, orch, **kw):
                calls["intercept"] += 1
                return InterceptDecision.REDIRECTED

        ctx = dataclasses.replace(build_default_context(None), slack_gate=_RedirectGate())
        set_context(ctx)
        try:
            event = {
                "user": "U123",
                "channel": "C456",
                "text": "hello",
                "ts": "9999.0001",
                "team": "T789",
            }
            mock_orch = AsyncMock()
            ch_cfg = MagicMock()
            ch_cfg.activation = "mention"
            mock_cfg = MagicMock()
            mock_cfg.channel_config.return_value = ch_cfg
            mock_orch._cfg = mock_cfg
            mock_orch.channel_history = None
            mock_orch.sessions = None
            mock_orch.conv_log = None
            mock_orch.slack = None

            seen = SeenCache()  # REAL cache so the dedup path is exercised
            with patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True), \
                 patch("kiro_crew.slack.events.sel") as mock_sel, \
                 patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
                 patch("kiro_crew.slack.events.is_owner", return_value=True), \
                 patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_handle:
                audits = []
                mock_sel.return_value.log_api_access = lambda **kw: audits.append(kw)

                await _route_message(mock_orch, event, seen, is_mention=True)
                # Short-circuited: the inline handler never ran.
                mock_handle.assert_not_called()
                # The intercept decision was audited (distinct from the allowlist audit).
                assert any(a.get("operation") == "slack.message.intercept" for a in audits)

                # Retry of the SAME event: the adapter must NOT be invoked again
                # (dedup), and still no inline handling.
                await _route_message(mock_orch, event, seen, is_mention=True)
                assert calls["intercept"] == 1, "redirect adapter re-invoked on retry (no dedup)"
                mock_handle.assert_not_called()
        finally:
            set_context(None)

    @pytest.mark.asyncio
    async def test_interceptor_raising_gate_fails_closed_to_dropped(self):
        """A composed gate whose intercept_message RAISES must fail CLOSED
        (CWE-1188 / deny-by-default): safe_context_call degrades the decision to
        InterceptDecision.DROPPED, _route_message short-circuits before the
        inline handler runs, and the drop reaches the SEL audit."""
        import dataclasses
        from unittest.mock import MagicMock

        from kiro_crew.platform import build_default_context
        from kiro_crew.platform.context import set_context
        from kiro_crew.slack.events import SeenCache, _route_message

        class _RaisingGate:
            def validate_enterprise(self, *a, **k):
                return True

            def check_message_origin(self, *a, **k):
                return True

            def heartbeat_safe_tools(self):
                return frozenset()

            def intercept_message(self, orch, **kw):
                raise RuntimeError("gate boom")

        ctx = dataclasses.replace(build_default_context(None), slack_gate=_RaisingGate())
        set_context(ctx)
        try:
            event = {
                "user": "U123",
                "channel": "C456",
                "text": "hello",
                "ts": "8888.0001",
                "team": "T789",
            }
            mock_orch = AsyncMock()
            ch_cfg = MagicMock()
            ch_cfg.activation = "mention"
            mock_cfg = MagicMock()
            mock_cfg.channel_config.return_value = ch_cfg
            mock_orch._cfg = mock_cfg
            mock_orch.channel_history = None
            mock_orch.sessions = None
            mock_orch.conv_log = None
            mock_orch.slack = None

            seen = SeenCache()
            with patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True), \
                 patch("kiro_crew.slack.events.sel") as mock_sel, \
                 patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
                 patch("kiro_crew.slack.events.is_owner", return_value=True), \
                 patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_handle:
                audits = []
                mock_sel.return_value.log_api_access = lambda **kw: audits.append(kw)

                await _route_message(mock_orch, event, seen, is_mention=True)
                # Fail-closed: the raising gate degraded to DROPPED, so the inline
                # handler never ran.
                mock_handle.assert_not_called()
                intercepts = [
                    a for a in audits if a.get("operation") == "slack.message.intercept"
                ]
                assert intercepts, "intercept decision was not audited"
                assert "dropped" in (intercepts[0].get("error") or "")
        finally:
            set_context(None)

    @pytest.mark.asyncio
    async def test_interceptor_dropped_decision_short_circuits(self):
        """A gate returning InterceptDecision.DROPPED directly must
        short-circuit _route_message (the inline handler never runs) and audit
        the drop (CWE-1188 / deny-by-default)."""
        import dataclasses
        from unittest.mock import MagicMock

        from kiro_crew.platform import build_default_context
        from kiro_crew.platform.context import set_context
        from kiro_crew.platform.interfaces import InterceptDecision
        from kiro_crew.slack.events import SeenCache, _route_message

        class _DropGate:
            def validate_enterprise(self, *a, **k):
                return True

            def check_message_origin(self, *a, **k):
                return True

            def heartbeat_safe_tools(self):
                return frozenset()

            def intercept_message(self, orch, **kw):
                return InterceptDecision.DROPPED

        ctx = dataclasses.replace(build_default_context(None), slack_gate=_DropGate())
        set_context(ctx)
        try:
            event = {
                "user": "U123",
                "channel": "C456",
                "text": "hello",
                "ts": "7777.0001",
                "team": "T789",
            }
            mock_orch = AsyncMock()
            ch_cfg = MagicMock()
            ch_cfg.activation = "mention"
            mock_cfg = MagicMock()
            mock_cfg.channel_config.return_value = ch_cfg
            mock_orch._cfg = mock_cfg
            mock_orch.channel_history = None
            mock_orch.sessions = None
            mock_orch.conv_log = None
            mock_orch.slack = None

            seen = SeenCache()
            with patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True), \
                 patch("kiro_crew.slack.events.sel") as mock_sel, \
                 patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
                 patch("kiro_crew.slack.events.is_owner", return_value=True), \
                 patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_handle:
                audits = []
                mock_sel.return_value.log_api_access = lambda **kw: audits.append(kw)

                await _route_message(mock_orch, event, seen, is_mention=True)
                mock_handle.assert_not_called()
                intercepts = [
                    a for a in audits if a.get("operation") == "slack.message.intercept"
                ]
                assert intercepts, "intercept decision was not audited"
                assert "dropped" in (intercepts[0].get("error") or "")
        finally:
            set_context(None)

    def test_blocks_extraction_recovers_all_element_types(self):
        """Verify _extract_blocks_text handles mixed element types correctly."""
        event_blocks = [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "Hello "},
                            {"type": "user", "user_id": "U999"},
                            {"type": "text", "text": " check "},
                            {"type": "channel", "channel_id": "C111"},
                        ],
                    },
                ],
            }
        ]
        recovered = _extract_blocks_text(event_blocks)
        assert "Hello " in recovered
        assert "<@U999>" in recovered
        assert "<#C111>" in recovered

    def test_fallback_placeholder_dropped_when_extraction_fails(self):
        """When blocks are empty/unrecoverable and text is a placeholder,
        text should become empty string so the message is dropped."""
        event_text = "This message contains interactive elements."
        assert event_text in _SLACK_BLOCK_FALLBACKS

        event_blocks = [
            {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Click"}}]}
        ]
        extracted = _extract_blocks_text(event_blocks)
        assert extracted == ""


class TestThreadFollowAddressedToOthers:
    """Thread-follow must not answer a reply addressed to someone else.

    In a thread the bot follows (mention, review, or observe mode, thread_follow
    on), a reply that opens with @-mentions of other users or bots, none of them
    this bot, is theirs and is skipped. A reply with no leading mention, one that
    leads with this bot, one that only names someone in passing, or one routed
    while this bot's own user id is unknown is still dispatched.
    """

    SELF_UID = "U0SELF"
    OTHER_UID = "U0OTHER"
    GATE_DENIAL = "thread-follow: addressed to another user"

    def _orch(self, activation: str) -> MagicMock:
        orch = AsyncMock()
        cfg = MagicMock()
        cfg.channel_config.return_value = ChannelConfig(activation=activation, thread_follow=True)
        orch._cfg = cfg
        orch.channel_history = None
        orch.sessions = MagicMock()
        orch.sessions.has_session.return_value = True
        orch.sessions.enqueue.return_value = False
        orch.sessions.dequeue.return_value = None
        orch.conv_log = None
        orch.slack = None
        orch._session_tasks = {}
        orch._handler_tasks = set()
        orch._pending_queue = {}
        return orch

    def _seen(self) -> MagicMock:
        seen = MagicMock()
        seen.check_and_add = lambda x: False
        seen.check = lambda x: False
        seen.add = lambda x: None
        return seen

    async def _route(
        self,
        text: str,
        self_uid: str,
        activation: str = ACTIVATION_MENTION,
        extra: dict | None = None,
    ) -> tuple[list[str], AsyncMock]:
        """Route one thread reply; return its ``slack.message`` denials and the dispatch mock."""
        denials: list[str] = []

        def _log(**kw):
            if kw.get("operation") == "slack.message" and kw.get("outcome") == "denied":
                denials.append(kw.get("error", ""))

        event = {
            "user": "U0HUMAN",
            "channel": "C0MAIN",
            "text": text,
            "ts": "1700.2",
            "thread_ts": "1700.1",
            "team": "T0TEAM",
            **(extra or {}),
        }
        orch = self._orch(activation)
        dispatch = AsyncMock()
        with (
            patch("kiro_crew.slack.events.sel") as mock_sel,
            patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
            patch("kiro_crew.slack.events.is_owner", return_value=True),
            patch("kiro_crew.slack.events.validated_self_user_id", return_value=self_uid),
            patch("kiro_crew.slack.events.handle_message", dispatch),
        ):
            mock_sel.return_value.log_api_access = _log
            await _route_message(orch, event, self._seen(), is_mention=False)
            if orch._handler_tasks:
                await asyncio.gather(*orch._handler_tasks, return_exceptions=True)
        return denials, dispatch

    async def _assert_skipped(self, text: str, activation: str = ACTIVATION_MENTION) -> None:
        denials, dispatch = await self._route(text, self.SELF_UID, activation)
        assert denials == [self.GATE_DENIAL]
        dispatch.assert_not_called()

    async def _assert_answered(
        self, text: str, self_uid: str = SELF_UID, activation: str = ACTIVATION_MENTION
    ) -> None:
        denials, dispatch = await self._route(text, self_uid, activation)
        assert denials == []
        dispatch.assert_awaited_once()
        assert dispatch.await_args.args[3] == text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "activation", [ACTIVATION_MENTION, ACTIVATION_REVIEW, ACTIVATION_OBSERVE]
    )
    async def test_reply_addressed_to_another_bot_is_not_answered(self, activation):
        text = f"<@{self.OTHER_UID}> the issue has been fixed, please verify"
        await self._assert_skipped(text, activation)

    @pytest.mark.asyncio
    async def test_reply_with_labelled_leading_mention_of_another_user_is_not_answered(self):
        await self._assert_skipped(f"  <@{self.OTHER_UID}|other> please verify")

    @pytest.mark.asyncio
    async def test_recovered_forward_text_addressed_to_another_user_is_not_answered(self):
        """The check reads the text recovered from a forward, not the placeholder."""
        forward = {"attachments": [{"is_share": True, "text": f"<@{self.OTHER_UID}> verify"}]}
        denials, dispatch = await self._route(
            "This message contains interactive elements.", self.SELF_UID, extra=forward
        )
        assert denials == [self.GATE_DENIAL]
        dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_reply_leading_with_several_other_mentions_is_not_answered(self):
        await self._assert_skipped(f"<@{self.OTHER_UID}> <@U0THIRD> please verify")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "activation", [ACTIVATION_MENTION, ACTIVATION_REVIEW, ACTIVATION_OBSERVE]
    )
    async def test_reply_without_mentions_is_still_answered(self, activation):
        await self._assert_answered("thanks, can you also add a test?", activation=activation)

    @pytest.mark.asyncio
    async def test_reply_naming_another_user_in_passing_is_answered(self):
        await self._assert_answered(f"please retry the deploy, cc <@{self.OTHER_UID}>")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "activation", [ACTIVATION_MENTION, ACTIVATION_REVIEW, ACTIVATION_OBSERVE]
    )
    async def test_reply_leading_with_self_and_another_is_answered(self, activation):
        text = f"<@{self.SELF_UID}> <@{self.OTHER_UID}> both take a look"
        await self._assert_answered(text, activation=activation)

    @pytest.mark.asyncio
    async def test_unknown_self_id_keeps_existing_behaviour(self):
        await self._assert_answered(f"<@{self.OTHER_UID}> please verify", self_uid="")
