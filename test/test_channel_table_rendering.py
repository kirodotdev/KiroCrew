"""Per-target Markdown table rendering, end to end through each renderer.

The pure formatter's own contract lives in ``test_messaging_tables.py``. What
this file pins is the part a formatter test cannot see: WHICH delivery target
converts, that the turn's canonical text does not, and that a target declaring
``native`` without the capability still cannot ship raw pipes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.messaging.driver import APPROVAL_AUTO, TurnDriver
from kiro_crew.messaging.split import iter_fence_spans
from kiro_crew.messaging.tables import (
    TABLE_POLICY_AUTO,
    TABLE_POLICY_NATIVE,
    TABLE_POLICY_OFF,
    display_width,
    resolve_table_policy,
)
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.slack.transport import SLACK_CAPABILITIES
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.webex.client import WEBEX_MAX_TEXT
from kiro_crew.webex.renderer import WebexRenderer
from kiro_crew.webex.transport import WEBEX_CAPABILITIES
from kiro_crew.wecom.renderer import WeComRenderer
from kiro_crew.wecom.transport import WECOM_CAPABILITIES
from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
from kiro_crew.weixin.turn_renderer import WeixinRenderer

#: A table wide enough (60 display columns) that ``auto`` must choose cards.
WIDE_TABLE = (
    "| Provider | Auth | Status | Notes |\n"
    "| --- | --- | --- | --- |\n"
    "| GitHub | OAuth app | Gated | needs an installed app first |"
)
WIDE_CARDS = (
    "**GitHub**\n" "- Auth: OAuth app\n" "- Status: Gated\n" "- Notes: needs an installed app first"
)

#: 29 display columns, so ``auto`` keeps it a grid.
NARROW_TABLE = "| Provider | Auth | Status |\n| --- | --- | --- |\n| GitHub | OAuth app | Gated |"


def _tall_narrow_table(rows: int) -> str:
    body = "\n".join(f"| Row {index:03d} | ok |" for index in range(rows))
    return "| Name | State |\n| --- | --- |\n" + body


def _expanding_wide_table(rows: int) -> str:
    body = "\n".join(
        f"| Provider {index:03d} | OAuth app | Gated | needs install |" for index in range(rows)
    )
    return "| Provider | Auth | Status | Notes |\n" "| --- | --- | --- | --- |\n" + body


def _table(width_a: int, width_b: int) -> str:
    """A 2-column table whose grid is exactly ``width_a + width_b + 3`` wide."""
    return (
        f"| {'A' * width_a} | {'B' * width_b} |\n"
        "| --- | --- |\n"
        f"| {'a' * width_a} | {'b' * width_b} |"
    )


def _ends_inside_fence(body: str) -> bool:
    """True when *body* leaves a fence open, judged by the shared fence grammar.

    Probes with a trailing plain line: the shared splitter's span view reports
    it as code only when the body never closed its fence. Asking the one
    grammar beats re-deriving a delimiter rule that could disagree with it.
    """
    probe = f"{body}\nx"
    offset = len(probe) - 1
    return any(start <= offset < end for start, end in iter_fence_spans(probe))


class _Provider:
    """Scripted event stream. Never spawns a real kiro-cli."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def stream(self, message: str) -> Any:
        for chunk in self._chunks:
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=chunk)
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id: Any, *, always: bool = False) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None


# ── channel fakes ─────────────────────────────────────────────────────────


class FakeDiscordClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.components: list[list[dict] | None] = []
        self._n = 0

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def send_message(self, channel_id: str, text: str, **kw: Any) -> str:
        self.sent.append(text)
        self.components.append(kw.get("components"))
        self._n += 1
        return f"m{self._n}"

    async def edit_message(self, channel_id: str, message_id: str, text: str, **kw: Any) -> bool:
        self.edits.append(text)
        self.components.append(kw.get("components"))
        return True

    def delivered(self) -> list[str]:
        """Every distinct body Discord was asked to display, newest edit last."""
        return self.sent + self.edits

    def final(self) -> str:
        return (self.edits or self.sent or [""])[-1]

    def button_labels(self) -> list[str]:
        rows = [c for c in self.components if c]
        return [
            btn.get("label", "")
            for row in (rows[-1] if rows else [])
            for btn in row.get("components", [])
        ]


class FakeTeamsClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        return None

    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        self.sent.append(content)
        return f"m{len(self.sent)}"


class FakeWebexClient:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []

    async def send_message(self, conversation_id: str, markdown: str, **kw: Any) -> str:
        self.sent.append(markdown)
        return f"m{len(self.sent)}"

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        self.edits.append(markdown)
        return True

    async def delete_message(self, message_id: str) -> None:
        return None

    def final(self) -> str:
        return (self.edits or self.sent or [""])[-1]


class FakeWeComClient:
    def __init__(self) -> None:
        self.frames: list[tuple[str, bool]] = []

    async def send_stream(self, req_id: str, stream_id: str, content: str, *, finish: bool) -> bool:
        self.frames.append((content, finish))
        return True

    async def send_reply(self, url: str, content: str) -> None:
        self.frames.append((content, True))

    def final(self) -> str:
        return self.frames[-1][0] if self.frames else ""


class FakeWeixinClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, *, to: str, text: str, **kw: Any) -> None:
        self.sent.append(text)

    async def send_typing(self, *a: Any, **kw: Any) -> None:
        return None


class _WeixinCtx:
    def get(self, account_id: str, to: str) -> str:
        return "ctx"


# ── per-channel drivers ───────────────────────────────────────────────────


async def _discord_final(text: str, *, chunks: list[str] | None = None) -> str:
    client = FakeDiscordClient()
    renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
    for chunk in chunks or [text]:
        await renderer.on_text_chunk(chunk)
    await renderer.on_done()
    return client.final()


async def _teams_final(text: str) -> str:
    client = FakeTeamsClient()
    renderer = TeamsRenderer(client, "conv", "https://svc.test/", TEAMS_CAPABILITIES)
    await renderer.on_text_chunk(text)
    await renderer.on_done()
    return client.sent[-1]


async def _webex_final(text: str) -> str:
    client = FakeWebexClient()
    renderer = WebexRenderer(client, "room", WEBEX_CAPABILITIES)
    await renderer.on_text_chunk(text)
    await renderer.on_done()
    return client.final()


async def _weixin_sent(text: str) -> list[str]:
    client = FakeWeixinClient()
    renderer = WeixinRenderer(
        client,
        "user",
        WEIXIN_CAPABILITIES,
        ctx_store=_WeixinCtx(),
        account_id="acct",
    )
    await renderer.on_text_chunk(text)
    await renderer.on_done()
    return client.sent


#: The unsupported-channel set: every one converts.
_UNSUPPORTED = [
    pytest.param(_discord_final, id="discord"),
    pytest.param(_teams_final, id="teams"),
    pytest.param(_webex_final, id="webex"),
]


class TestCapabilityDeclarations:
    def test_split_capable_channels_declare_adaptive_non_native_tables(self) -> None:
        for caps in (
            DISCORD_CAPABILITIES,
            TEAMS_CAPABILITIES,
            WEBEX_CAPABILITIES,
        ):
            assert caps.table_mode == TABLE_POLICY_AUTO
            assert caps.native_tables is False

    def test_wecom_preserves_canonical_tables_for_its_single_bubble(self) -> None:
        assert WECOM_CAPABILITIES.table_mode == TABLE_POLICY_OFF
        assert WECOM_CAPABILITIES.native_tables is False

    def test_telegram_and_weixin_declare_native_tables(self) -> None:
        # Telegram's sendRichMessage renders a real table; iLink renders
        # Markdown natively, which weixin's render_chunks preserves.
        for caps in (TELEGRAM_CAPABILITIES, WEIXIN_CAPABILITIES):
            assert caps.table_mode == TABLE_POLICY_NATIVE
            assert caps.native_tables is True

    def test_slack_keeps_its_existing_table_path(self) -> None:
        # Slack renders no table, but its established formatter flattens before
        # send, so the shared table renderer remains off for this target.
        assert SLACK_CAPABILITIES.table_mode == TABLE_POLICY_OFF
        assert SLACK_CAPABILITIES.native_tables is False

    def test_the_conservative_floor_is_explicitly_off_and_non_native(self) -> None:
        caps = TransportCapabilities()
        assert caps.table_mode == TABLE_POLICY_OFF
        assert caps.native_tables is False

    def test_serialization_carries_mode_and_native_support(self) -> None:
        assert TransportCapabilities().to_dict()["table_mode"] == TABLE_POLICY_OFF
        assert TransportCapabilities().to_dict()["native_tables"] is False
        caps = TransportCapabilities(table_mode=TABLE_POLICY_NATIVE, native_tables=True)
        assert caps.to_dict()["table_mode"] == TABLE_POLICY_NATIVE
        assert caps.to_dict()["native_tables"] is True


class TestOutboundTableSafety:
    def test_cards_rescan_a_joined_authorization_header_and_bearer_value(self) -> None:
        from kiro_crew.messaging.tables import TABLE_POLICY_CARDS
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        token = "opaque-token-value"
        table = (
            "| Request | Authorization | Notes |\n"
            "| --- | --- | --- |\n"
            f"| outbound | Bearer {token} | sent to provider |"
        )
        first_pass, _ = redact_exfiltration_urls(table)
        first_pass, _ = redact_credentials(first_pass)
        assert token in first_pass

        caps = replace(TEAMS_CAPABILITIES, table_mode=TABLE_POLICY_CARDS)
        renderer = TeamsRenderer(FakeTeamsClient(), "c", "https://s.test/", caps)
        rendered = renderer.render_tables_for_target(first_pass)

        assert token not in rendered
        assert "[REDACTED: credential]" in rendered

    def test_an_empty_row_cannot_downgrade_a_credential_card_to_a_grid(self) -> None:
        token = "opaque-token-value"
        table = (
            "| Request | Authorization | Notes |\n"
            "| --- | --- | --- |\n"
            f"| outbound | Bearer {token} | {'x' * 30} |\n"
            "|  |  |  |"
        )
        renderer = TeamsRenderer(FakeTeamsClient(), "c", "https://s.test/", TEAMS_CAPABILITIES)

        rendered = renderer.render_tables_for_target(table)

        assert token not in rendered
        assert "[REDACTED: credential]" in rendered
        assert "—" in rendered

    def test_converted_output_is_rescanned_for_exfiltration_urls(self) -> None:
        from kiro_crew.messaging.tables import TABLE_POLICY_CARDS

        url = "https://attacker.example/c?x=" + "q" * 300
        table = (
            "| Request | Result | Notes |\n"
            "| --- | --- | --- |\n"
            f"| outbound | {url} | sent to provider |"
        )
        caps = replace(TEAMS_CAPABILITIES, table_mode=TABLE_POLICY_CARDS)
        renderer = TeamsRenderer(FakeTeamsClient(), "c", "https://s.test/", caps)
        rendered = renderer.render_tables_for_target(table)

        assert url not in rendered
        assert "[REDACTED:" in rendered


class TestPerTargetPolicies:
    @pytest.mark.parametrize(
        "caps",
        [
            DISCORD_CAPABILITIES,
            TEAMS_CAPABILITIES,
            WEBEX_CAPABILITIES,
        ],
    )
    def test_unsupported_targets_resolve_to_the_adaptive_policy(
        self, caps: TransportCapabilities
    ) -> None:
        assert caps.table_mode == TABLE_POLICY_AUTO
        assert (
            resolve_table_policy(caps.table_mode, native_tables=caps.native_tables)
            == TABLE_POLICY_AUTO
        )

    @pytest.mark.parametrize("caps", [TELEGRAM_CAPABILITIES, WEIXIN_CAPABILITIES])
    def test_native_targets_resolve_to_pass_through(self, caps: TransportCapabilities) -> None:
        assert caps.table_mode == TABLE_POLICY_NATIVE
        assert (
            resolve_table_policy(caps.table_mode, native_tables=caps.native_tables)
            == TABLE_POLICY_OFF
        )

    def test_slack_keeps_its_own_flattening(self) -> None:
        # Slack's tables are already flattened on the render path
        # (slack/format.py::_convert_tables), whose output the golden-transcript
        # harness pins byte-for-byte. ``off`` keeps this conversion out of that
        # path entirely.
        assert SLACK_CAPABILITIES.table_mode == TABLE_POLICY_OFF
        assert (
            resolve_table_policy(
                SLACK_CAPABILITIES.table_mode,
                native_tables=SLACK_CAPABILITIES.native_tables,
            )
            == TABLE_POLICY_OFF
        )

    def test_slack_flattening_is_byte_unchanged(self) -> None:
        from kiro_crew.slack.format import to_slack_mrkdwn

        assert to_slack_mrkdwn(WIDE_TABLE) == (
            "• *Provider:* GitHub | *Auth:* OAuth app | *Status:* Gated "
            "| *Notes:* needs an installed app first"
        )


class TestCanonicalTextIsUnaffected:
    @pytest.mark.asyncio
    async def test_the_driver_returns_the_authored_table_not_a_rendering(self) -> None:
        # TurnDriver's return value is what the transcript, the dashboard and
        # history record. A per-target presentation choice must not reach it.
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        driver = TurnDriver(_Provider([WIDE_TABLE]), renderer, approval_mode=APPROVAL_AUTO)
        canonical = await driver.run("hi")
        assert canonical == WIDE_TABLE
        # ...while what Discord was asked to display is the card rendering.
        assert client.final() == WIDE_CARDS

    @pytest.mark.asyncio
    async def test_the_renderer_text_accessors_stay_canonical(self) -> None:
        # These feed history as well as the final send, so converting inside
        # them would rewrite the stored answer.
        teams = TeamsRenderer(FakeTeamsClient(), "c", "https://s.test/", TEAMS_CAPABILITIES)
        await teams.on_text_chunk(WIDE_TABLE)
        assert teams.text() == WIDE_TABLE

        webex = WebexRenderer(FakeWebexClient(), "room", WEBEX_CAPABILITIES)
        await webex.on_text_chunk(WIDE_TABLE)
        assert webex.text() == WIDE_TABLE

        wecom = WeComRenderer(FakeWeComClient(), "rq", "https://r.test", WECOM_CAPABILITIES)
        await wecom.on_text_chunk(WIDE_TABLE)
        assert wecom.text() == WIDE_TABLE


class TestDiscordProtocolIsolation:
    _TABLE = f"| {'A' * 32} | [OPTIONS |\n" "| --- | --- |\n" "| choose | retry \\| cancel] |"
    _STEERING_TABLE = (
        f"| {'A' * 32} | [STEERING steer-deadbeef |\n"
        "| --- | --- |\n"
        "| choose | folded] |\n\nSettled prose."
    )
    _INCOMPLETE_OPTIONS_TABLE = (
        f"| {'A' * 32} | [OPTIONS | ] |\n"
        "| --- | --- | --- |\n"
        "| choose | retry | |\n\nSettled prose."
    )

    @staticmethod
    def _assert_table_stays_content(client: FakeDiscordClient) -> None:
        assert client.button_labels() == []
        assert "[OPTIONS: retry | cancel]" in client.final()

    @pytest.mark.asyncio
    async def test_final_table_rendering_cannot_synthesize_option_buttons(self) -> None:
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk(self._TABLE)

        await renderer.on_done()

        self._assert_table_stays_content(client)

    @pytest.mark.asyncio
    async def test_steer_seal_table_rendering_cannot_synthesize_option_buttons(self) -> None:
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk(self._TABLE)

        await renderer.on_steer_consumed()

        self._assert_table_stays_content(client)

    @pytest.mark.asyncio
    async def test_settled_table_rendering_cannot_synthesize_a_steering_boundary(self) -> None:
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk(self._STEERING_TABLE)

        assert "".join(renderer._buf) == self._STEERING_TABLE
        assert "[STEERING steer-deadbeef: folded]" in client.final()
        assert renderer._seal_count == 0

        await renderer.on_done()

        delivered = "\n".join(client.delivered())
        assert "[STEERING steer-deadbeef: folded]" in delivered
        assert "Settled prose." in client.final()
        assert renderer._seal_count == 0

    @pytest.mark.asyncio
    async def test_live_table_rendering_cannot_synthesize_incomplete_options(self) -> None:
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")

        await renderer.on_text_chunk(self._INCOMPLETE_OPTIONS_TABLE)

        assert "".join(renderer._buf) == self._INCOMPLETE_OPTIONS_TABLE
        assert "[OPTIONS: retry" in client.final()
        assert "Settled prose." in client.final()
        assert client.button_labels() == []


class TestUnsupportedTargetsConvert:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_a_wide_table_arrives_as_cards(self, deliver: Any) -> None:
        assert await deliver(WIDE_TABLE) == WIDE_CARDS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_no_literal_pipes_reach_a_wide_table_target(self, deliver: Any) -> None:
        assert "|" not in await deliver(WIDE_TABLE)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_cards_fit_a_forty_column_mobile_layout(self, deliver: Any) -> None:
        out = await deliver(WIDE_TABLE)
        widths = [display_width(line) for line in out.split("\n")]
        assert max(widths) <= 40, f"delivered card lines exceed ~40 columns: {widths}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_a_narrow_table_arrives_as_an_aligned_grid(self, deliver: Any) -> None:
        out = await deliver(NARROW_TABLE)
        assert out.startswith("```\n") and out.endswith("\n```")
        assert "Provider | Auth      | Status" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_prose_is_delivered_unchanged(self, deliver: Any) -> None:
        prose = "Pick a or b. A pipe | in prose stays.\n\nSecond paragraph."
        assert await deliver(prose) == prose

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_a_malformed_table_is_delivered_unchanged(self, deliver: Any) -> None:
        # 3-cell header against a 2-cell separator: not a table anywhere.
        malformed = "| a | b | c |\n| --- | --- |\n| 1 | 2 | 3 |"
        assert await deliver(malformed) == malformed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_a_fenced_table_example_is_delivered_unchanged(self, deliver: Any) -> None:
        fenced = "How to write one:\n\n```markdown\n" + WIDE_TABLE + "\n```\n\nThat is all."
        assert await deliver(fenced) == fenced

    @pytest.mark.asyncio
    @pytest.mark.parametrize("deliver", _UNSUPPORTED)
    async def test_a_tilde_fenced_table_example_is_delivered_unchanged(self, deliver: Any) -> None:
        fenced = "~~~\n" + WIDE_TABLE + "\n~~~"
        assert await deliver(fenced) == fenced


class TestAutoBoundaryThroughARenderer:
    @pytest.mark.asyncio
    async def test_forty_two_display_columns_stays_a_grid(self) -> None:
        out = await _teams_final(_table(20, 19))
        assert out.startswith("```\n")
        assert max(display_width(line) for line in out.split("\n")) == 42

    @pytest.mark.asyncio
    async def test_forty_three_display_columns_becomes_cards(self) -> None:
        out = await _teams_final(_table(20, 20))
        assert not out.startswith("```")
        assert out.startswith("**" + "a" * 20 + "**")


class TestFenceProbe:
    def test_the_probe_reports_only_an_unclosed_fence(self) -> None:
        ticks3, ticks4 = "`" * 3, "`" * 4
        assert _ends_inside_fence(f"{ticks3}\ncode")
        assert not _ends_inside_fence(f"{ticks3}\ncode\n{ticks3}")
        # A shorter run cannot close a longer opener, so the fence stays open.
        assert _ends_inside_fence(f"{ticks4}\ncode\n{ticks3}")
        assert not _ends_inside_fence("just prose")


class TestDeliveryFraming:
    @staticmethod
    def _header_only_table(headers: list[str]) -> str:
        return "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |"

    @staticmethod
    def _credential_table(rows: int) -> tuple[str, str]:
        token = "opaque-token-value"
        body = [f"| Row 000 | Bearer {token} |"]
        body.extend(f"| Row {index:03d} | ok |" for index in range(1, rows))
        table = "| Request | Authorization |\n| --- | --- |\n" + "\n".join(body)
        return table, token

    def test_discord_cards_fallback_keeps_post_transform_redaction(self) -> None:
        table, token = self._credential_table(60)
        caps = replace(DISCORD_CAPABILITIES, max_message_chars=600)
        renderer = DiscordRenderer(FakeDiscordClient(), "chan", caps, session_key="sk")

        rendered = renderer._render_tables_for_delivery(table, final=True)

        assert "```" not in rendered
        assert token not in rendered
        assert "[REDACTED: credential]" in rendered

    @pytest.mark.asyncio
    async def test_teams_cards_fallback_keeps_post_transform_redaction(self) -> None:
        table, token = self._credential_table(12)
        caps = replace(TEAMS_CAPABILITIES, max_message_chars=120)
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        await renderer.on_text_chunk(table)
        await renderer.on_done()

        delivered = "".join(client.sent)
        assert token not in delivered
        assert "[REDACTED: credential]" in delivered

    @pytest.mark.asyncio
    async def test_webex_cards_fallback_keeps_post_transform_redaction(self) -> None:
        table, token = self._credential_table(700)
        client = FakeWebexClient()
        renderer = WebexRenderer(client, "room", WEBEX_CAPABILITIES)
        await renderer.on_text_chunk(table)
        await renderer.on_done()

        delivered = "".join(client.sent + client.edits)
        assert token not in delivered
        assert "[REDACTED: credential]" in delivered

    @pytest.mark.asyncio
    async def test_discord_uses_raw_header_only_table_when_forced_cards_stay_a_grid(
        self,
    ) -> None:
        headers = ["```", *(f"Column {index:02d} " + "x" * 18 for index in range(12))]
        table = self._header_only_table(headers)
        caps = replace(DISCORD_CAPABILITIES, max_message_chars=600)
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", caps, session_key="sk")
        safe_raw = renderer.redact_for_target(table)
        converted = renderer.render_tables_for_target(table)
        assert len(safe_raw) <= renderer._limit() < len(converted)
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        assert client.final() == safe_raw

    @pytest.mark.asyncio
    async def test_teams_uses_raw_header_only_table_when_forced_cards_stay_a_grid(
        self,
    ) -> None:
        table = self._header_only_table(["a" * 25, "b" * 25, "c" * 25])
        caps = replace(TEAMS_CAPABILITIES, max_message_chars=120)
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        safe_raw = renderer.redact_for_target(table)
        converted = renderer.render_tables_for_target(table)
        assert len(safe_raw) <= caps.max_message_chars < len(converted)
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        assert client.sent == [safe_raw]

    @pytest.mark.asyncio
    async def test_webex_uses_raw_header_only_table_when_forced_cards_stay_a_grid(
        self,
    ) -> None:
        headers = [f"Column {index:03d} " + "x" * 24 for index in range(100)]
        table = self._header_only_table(headers)
        client = FakeWebexClient()
        renderer = WebexRenderer(client, "room", WEBEX_CAPABILITIES)
        safe_raw = renderer.redact_for_target(table)
        converted = renderer.render_tables_for_target(table)
        assert len(safe_raw.encode("utf-8")) <= WEBEX_MAX_TEXT < len(converted.encode("utf-8"))
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        assert client.sent == [safe_raw]

    @pytest.mark.asyncio
    async def test_teams_chunks_safe_raw_header_only_table_after_long_prose(
        self,
    ) -> None:
        table = self._header_only_table(["a" * 25, "b" * 25, "c" * 25])
        cap = TEAMS_CAPABILITIES.max_message_chars
        raw = "p" * (cap - 10) + "\n\n" + table
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", TEAMS_CAPABILITIES)
        safe_raw = renderer.redact_for_target(raw)
        converted = renderer.render_tables_for_target(raw)
        assert len(safe_raw) > cap
        assert len(converted) > cap
        assert "```" in converted
        await renderer.on_text_chunk(raw)

        await renderer.on_done()

        assert len(client.sent) > 1
        assert "".join(client.sent) == safe_raw
        assert all("```" not in chunk for chunk in client.sent)

    @pytest.mark.asyncio
    async def test_webex_chunks_safe_raw_header_only_table_after_long_prose(
        self,
    ) -> None:
        table = self._header_only_table(["a" * 25, "b" * 25, "c" * 25])
        raw = "p" * (WEBEX_MAX_TEXT - 10) + "\n\n" + table
        client = FakeWebexClient()
        renderer = WebexRenderer(client, "room", WEBEX_CAPABILITIES)
        safe_raw = renderer.redact_for_target(raw)
        converted = renderer.render_tables_for_target(raw)
        assert len(safe_raw.encode("utf-8")) > WEBEX_MAX_TEXT
        assert len(converted.encode("utf-8")) > WEBEX_MAX_TEXT
        assert "```" in converted
        await renderer.on_text_chunk(raw)

        await renderer.on_done()

        assert len(client.sent) > 1
        assert "".join(client.sent) == safe_raw
        assert all("```" not in chunk for chunk in client.sent)

    @pytest.mark.asyncio
    async def test_discord_keeps_valid_over_cap_cards_as_cards(self) -> None:
        table = _expanding_wide_table(10)
        probe = DiscordRenderer(FakeDiscordClient(), "chan", DISCORD_CAPABILITIES, session_key="sk")
        cards = probe.render_tables_for_target(table)
        limit = (len(table) + len(cards)) // 2
        caps = replace(DISCORD_CAPABILITIES, max_message_chars=limit + 100)
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", caps, session_key="sk")
        assert len(table) <= renderer._limit() < len(cards)
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        delivered = "\n".join(client.delivered())
        assert "**Provider 000**" in delivered and "**Provider 009**" in delivered
        assert "| Provider |" not in delivered

    @pytest.mark.asyncio
    async def test_teams_keeps_valid_over_cap_cards_as_cards(self) -> None:
        table = _expanding_wide_table(5)
        probe = TeamsRenderer(FakeTeamsClient(), "c", "https://s.test/", TEAMS_CAPABILITIES)
        cards = probe.render_tables_for_target(table)
        cap = (len(table) + len(cards)) // 2
        caps = replace(TEAMS_CAPABILITIES, max_message_chars=cap)
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        assert len(table) <= cap < len(cards)
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        delivered = "".join(client.sent)
        assert "**Provider 000**" in delivered and "**Provider 004**" in delivered
        assert "| Provider |" not in delivered

    @pytest.mark.asyncio
    async def test_webex_keeps_valid_over_cap_cards_as_cards(self) -> None:
        table = _expanding_wide_table(100)
        client = FakeWebexClient()
        renderer = WebexRenderer(client, "room", WEBEX_CAPABILITIES)
        cards = renderer.render_tables_for_target(table)
        assert len(table.encode("utf-8")) <= WEBEX_MAX_TEXT < len(cards.encode("utf-8"))
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        delivered = "".join(client.sent)
        assert "**Provider 000**" in delivered and "**Provider 099**" in delivered
        assert "| Provider |" not in delivered

    @pytest.mark.asyncio
    async def test_discord_cards_an_over_cap_variable_fence_grid(self) -> None:
        rows = ["| ``` |"] + [f"| Row {index:03d} |" for index in range(80)]
        table = "| Value |\n| --- |\n" + "\n".join(rows)
        caps = replace(DISCORD_CAPABILITIES, max_message_chars=600)
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", caps, session_key="sk")
        await renderer.on_text_chunk(table)
        assert client.delivered() == []

        await renderer.on_done()

        delivered = client.delivered()
        joined = "\n".join(delivered)
        assert delivered
        assert all(len(body) <= caps.max_message_chars for body in delivered)
        assert all(not _ends_inside_fence(body) for body in delivered)
        assert "**```**" in joined and "**Row 079**" in joined
        assert "| Row 079 |" not in joined

    @pytest.mark.asyncio
    async def test_teams_cards_a_grid_that_would_need_continuation_messages(self) -> None:
        caps = replace(TEAMS_CAPABILITIES, max_message_chars=120)
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        await renderer.on_text_chunk(_tall_narrow_table(30))

        await renderer.on_done()

        delivered = "".join(client.sent)
        assert all(len(chunk) <= 120 for chunk in client.sent)
        assert "```" not in delivered
        assert "**Row 000**" in delivered and "**Row 029**" in delivered

    @pytest.mark.asyncio
    async def test_webex_cards_a_grid_that_would_cross_its_byte_cap(self) -> None:
        client = FakeWebexClient()
        renderer = WebexRenderer(client, "room", WEBEX_CAPABILITIES)
        await renderer.on_text_chunk(_tall_narrow_table(700))

        await renderer.on_done()

        delivered = "".join(client.sent)
        assert all(len(chunk.encode("utf-8")) <= WEBEX_MAX_TEXT for chunk in client.sent)
        assert "```" not in delivered
        assert "**Row 000**" in delivered and "**Row 699**" in delivered

    @pytest.mark.asyncio
    async def test_wecom_keeps_canonical_table_when_safe_cards_would_exceed_cap(
        self,
    ) -> None:
        token = "opaque-token-value"
        body = [f"| Row 000 | Bearer {token} | {'x' * 30} |"]
        body.extend(f"| Row {index:03d} | ok |  |" for index in range(1, 700))
        table = "| Request | Authorization | Notes |\n" "| --- | --- | --- |\n" + "\n".join(body)
        cap = WECOM_CAPABILITIES.max_message_chars
        client = FakeWeComClient()
        renderer = WeComRenderer(client, "rq", "https://r.test", WECOM_CAPABILITIES)
        assert len(table) <= cap
        assert renderer.render_tables_for_target(table) == table
        await renderer.on_text_chunk(table)

        await renderer.on_done()

        assert client.final() == table
        assert "Row 699" in client.final()


class TestNativeTargetsAreUntouched:
    @pytest.mark.asyncio
    async def test_weixin_delivers_the_table_as_authored(self) -> None:
        sent = await _weixin_sent(WIDE_TABLE)
        assert sent == [WIDE_TABLE]

    @pytest.mark.asyncio
    async def test_weixin_narrow_table_is_not_gridded(self) -> None:
        assert await _weixin_sent(NARROW_TABLE) == [NARROW_TABLE]

    def test_telegram_conversion_is_a_no_op_at_the_seam(self) -> None:
        # Telegram's own HTML pipeline owns its table presentation; the shared
        # helper must hand its text back untouched.
        renderer = TelegramRenderer.__new__(TelegramRenderer)
        renderer.capabilities = TELEGRAM_CAPABILITIES
        assert renderer.render_tables_for_target(WIDE_TABLE) == WIDE_TABLE


class TestUnsupportedNativeIsCoercedNotLeakedAsPipes:
    @pytest.mark.asyncio
    async def test_declaring_native_on_a_pipes_only_target_yields_cards(self) -> None:
        caps = replace(TEAMS_CAPABILITIES, table_mode=TABLE_POLICY_NATIVE)
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        await renderer.on_text_chunk(WIDE_TABLE)
        await renderer.on_done()
        assert client.sent[-1] == WIDE_CARDS
        assert "|" not in client.sent[-1]

    @pytest.mark.asyncio
    async def test_the_coercion_ignores_the_width_threshold(self) -> None:
        # ``native`` resolves to cards, not to ``auto`` -- a narrow table is
        # carded too, because the request was "the target renders this itself"
        # and the answer is "it does not".
        caps = replace(TEAMS_CAPABILITIES, table_mode=TABLE_POLICY_NATIVE)
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        await renderer.on_text_chunk(NARROW_TABLE)
        await renderer.on_done()
        assert client.sent[-1] == "**GitHub**\n- Auth: OAuth app\n- Status: Gated"

    @pytest.mark.asyncio
    async def test_an_unknown_mode_still_converts_rather_than_leaking_pipes(self) -> None:
        caps = replace(TEAMS_CAPABILITIES, table_mode="vertical")
        client = FakeTeamsClient()
        renderer = TeamsRenderer(client, "c", "https://s.test/", caps)
        await renderer.on_text_chunk(WIDE_TABLE)
        await renderer.on_done()
        assert client.sent[-1] == WIDE_CARDS


class TestDiscordStreaming:
    @pytest.mark.asyncio
    async def test_a_still_arriving_table_is_not_frozen_half_built(self) -> None:
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk("| Provider | Auth | Status | Notes |\n| --- | --- |")
        await renderer.on_text_chunk(" --- | --- |\n| GitHub | OAuth app | Gated |")
        # Before the separator is complete the renderer cannot yet classify the
        # text as a table, so one partial raw live frame may exist. Once the run
        # is recognizable it must stop updating: no partial card is frozen and
        # no later raw rows are split into new messages.
        early = client.delivered()
        assert early and not any("**GitHub**" in body for body in early)
        await renderer.on_text_chunk(" needs an installed app first |")
        assert client.delivered() == early
        await renderer.on_done()
        assert client.final() == WIDE_CARDS

    @pytest.mark.asyncio
    async def test_prose_terminates_a_buffered_table_and_resumes_streaming(self) -> None:
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk(WIDE_TABLE)
        assert client.delivered() == []

        await renderer.on_text_chunk("\n\nDone.")

        assert client.final() == WIDE_CARDS + "\n\nDone."
        assert "| Provider |" not in client.final()

    @pytest.mark.asyncio
    async def test_conversion_happens_before_the_length_rotation(self) -> None:
        # Cards are longer than the pipes they replace, so converting only at
        # the send seam could seal a message past Discord's hard 2000-char cap.
        rows = "\n".join(
            f"| Provider {i:03d} | OAuth application | Gated | needs an installed app first |"
            for i in range(40)
        )
        table = "| Provider | Auth | Status | Notes |\n| --- | --- | --- | --- |\n" + rows
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk(table)
        # The run reaches the end of the active stream, so every row remains
        # buffered rather than splitting the table into raw-pipe fragments.
        assert client.delivered() == []
        await renderer.on_done()
        bodies = client.delivered()
        assert bodies, "the answer must be delivered"
        assert max(len(b) for b in bodies) <= DISCORD_CAPABILITIES.max_message_chars
        assert "**Provider 039**" in "".join(bodies), "the last row must still arrive"
        assert "| OAuth application |" not in "".join(bodies)

    @pytest.mark.asyncio
    async def test_an_over_limit_settled_table_defers_until_terminal_split(self) -> None:
        rows = "\n".join(
            f"| Provider {i:03d} | OAuth application | Gated | needs an installed app first |"
            for i in range(40)
        )
        table = "| Provider | Auth | Status | Notes |\n| --- | --- | --- | --- |\n" + rows
        text = table + "\n\nDone."
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")

        await renderer.on_text_chunk(text)

        assert client.delivered() == []
        assert "".join(renderer._buf) == text

        await renderer.on_done()

        bodies = client.delivered()
        assert (
            bodies and max(len(body) for body in bodies) <= DISCORD_CAPABILITIES.max_message_chars
        )
        assert "**Provider 039**" in "".join(bodies)
        assert "Done." in bodies[-1]

    @pytest.mark.asyncio
    async def test_a_trailing_options_trailer_survives_conversion(self) -> None:
        # The trailer holds pipes and sits directly under the table; absorbing
        # it as a body row would destroy the button row parsed from it.
        client = FakeDiscordClient()
        renderer = DiscordRenderer(client, "chan", DISCORD_CAPABILITIES, session_key="sk")
        await renderer.on_text_chunk(WIDE_TABLE + "\n[OPTIONS: retry | cancel]")
        await renderer.on_done()
        assert client.final() == WIDE_CARDS
        assert client.button_labels() == ["retry", "cancel"]
