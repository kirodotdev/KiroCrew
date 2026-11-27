"""The Slack reporters of the dashboard link-click window must not overstate it.

``generate_token`` mints ``exp = now + min(LINK_WINDOW_SECS, session_ttl)`` so a
raw link can never authenticate past the session it grants. Both Slack surfaces
that *report* that window — the DM sent by
:func:`kiro_crew.slack.allowlist.send_dashboard_link` and the ephemeral block
built by :func:`kiro_crew.slack.events._handle_dashboard` — printed the
unclamped constant, so ``/kirocrew dashboard 1m`` told the user "Click within
5m" about a link that dies in one.

Each test asserts the reported minutes against the minted token's own ``exp``,
not against a second hard-coded constant, so the assertion cannot pass by
agreeing with a copy of the bug. A full-length caller is the control: its text
is unchanged.
"""

from __future__ import annotations

import json
import os
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, _b64url_decode


def _click_window_secs(url: str) -> float:
    """Return the real click window of the token carried in *url*."""
    token = url.split("token=", 1)[1].split("&", 1)[0]
    payload = json.loads(_b64url_decode(token.split(".", 1)[0]))
    return float(payload["exp"]) - float(payload["iat"])


def _reported_link_mins(text: str) -> int:
    """Return N from ``⏱ Click within Nm · session lasts ...``."""
    return int(text.split("Click within ", 1)[1].split("m", 1)[0])


async def _send_dm(ttl: int) -> str:
    """Drive ``send_dashboard_link`` for *ttl* and return the DM body."""
    from kiro_crew.slack import allowlist as al

    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D_DM")
    slack.post_message = AsyncMock(return_value=None)

    cfg = MagicMock()
    cfg.dashboard.url = "http://127.0.0.1:5476"
    cfg.slack.use_tunnel_url = False

    with (
        patch.object(al.KiroCrewConfig, "load", return_value=cfg),
        patch.object(al, "get_tunnel_url", return_value=""),
        patch.object(al, "sel", return_value=MagicMock()),
        patch("kiro_crew.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
        patch("kiro_crew.dashboard.origin.socket.getaddrinfo", side_effect=socket.gaierror),
        patch.dict(os.environ, {}, KIROCREW_PORT=""),
    ):
        await al.send_dashboard_link(slack, "U1", ttl=ttl)

    return str(slack.post_message.call_args[0][1])


class TestSendDashboardLinkDm:
    @pytest.mark.asyncio
    async def test_a_short_session_is_not_promised_the_full_click_window(self) -> None:
        dm = await _send_dm(60)
        assert _reported_link_mins(dm) == 1
        assert "session lasts 1m" in dm

    @pytest.mark.asyncio
    async def test_the_reported_click_window_matches_the_minted_token(self) -> None:
        """The DM's countdown is derived from the same clamp the token uses."""
        from kiro_crew.slack import allowlist as al

        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_DM")
        slack.post_message = AsyncMock(return_value=None)

        cfg = MagicMock()
        cfg.dashboard.url = "http://127.0.0.1:5476"
        cfg.slack.use_tunnel_url = False

        with (
            patch.object(al.KiroCrewConfig, "load", return_value=cfg),
            patch.object(al, "get_tunnel_url", return_value=""),
            patch.object(al, "sel", return_value=MagicMock()),
            patch("kiro_crew.dashboard.origin.socket.gethostbyname", return_value="10.0.0.1"),
            patch("kiro_crew.dashboard.origin.socket.getaddrinfo", side_effect=socket.gaierror),
            patch.dict(os.environ, {}, KIROCREW_PORT=""),
        ):
            url = await al.send_dashboard_link(slack, "U1", ttl=120)

        dm = str(slack.post_message.call_args[0][1])
        assert _reported_link_mins(dm) == int(_click_window_secs(url)) // 60

    @pytest.mark.asyncio
    async def test_a_full_length_session_still_reads_five_minutes(self) -> None:
        """Control: the clamp only ever tightens, so the ordinary text is unchanged."""
        dm = await _send_dm(3600)
        assert _reported_link_mins(dm) == LINK_WINDOW_SECS // 60
        assert "session lasts 60m" in dm


class TestHandleDashboardBlock:
    @staticmethod
    def _orch() -> MagicMock:
        orch = MagicMock()
        orch.slack = MagicMock()
        orch.slack_command = "kirocrew"
        return orch

    @pytest.mark.asyncio
    async def test_a_short_session_is_not_promised_the_full_click_window(self) -> None:
        from kiro_crew.slack import events as ev

        respond = AsyncMock()
        with patch.object(
            ev, "send_dashboard_link", new_callable=AsyncMock, return_value="https://x/?token=t"
        ):
            await ev._handle_dashboard(self._orch(), "U_OWNER", "1m", respond)

        text = respond.call_args.kwargs["blocks"][0]["text"]["text"]
        assert _reported_link_mins(text) == 1
        assert "session lasts 1m" in text

    @pytest.mark.asyncio
    async def test_a_full_length_session_still_reads_five_minutes(self) -> None:
        from kiro_crew.slack import events as ev

        respond = AsyncMock()
        with patch.object(
            ev, "send_dashboard_link", new_callable=AsyncMock, return_value="https://x/?token=t"
        ):
            await ev._handle_dashboard(self._orch(), "U_OWNER", "", respond)

        text = respond.call_args.kwargs["blocks"][0]["text"]["text"]
        assert _reported_link_mins(text) == LINK_WINDOW_SECS // 60


def test_the_helpers_read_a_real_payload() -> None:
    """Self-check: the decoder is exercised against a live mint, not a fixture."""
    from kiro_crew.dashboard.token_auth import generate_token

    before = time.time()
    window = _click_window_secs(f"https://x/?token={generate_token('u', ttl_seconds=90)}")
    assert time.time() >= before
    assert window == 90
