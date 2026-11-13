"""Google MCP consent links ask for offline access.

Neither MCP engine sends ``access_type=offline`` or ``prompt=consent``, so
Google issues no refresh token and the user signs in again every hour. Crew
sets both on the exact Google endpoint as each consent URL is read off the
wire. These tests pin the helper's match and each ingest point that calls it:

* ``AcpClient._drain_notifications`` (kiro-cli init drain: chat and the
  Connections card mint),
* ``AcpClient.stream_events`` (kiro-cli mid-turn frame),
* ``session_handle_mcp._accept_oauth_request`` (the shared runtime's session
  handle, which is also where a KAS ``_kiro/openExternalUrl`` link lands).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.oauth_offline import with_google_offline_access
from kiro_crew.acp.session_handle_mcp import _accept_oauth_request
from kiro_crew.acp.types import (
    EVENT_MCP_OAUTH_REQUEST,
    METHOD_MCP_OAUTH_REQUEST,
    JsonRpcMessage,
)

GOOGLE_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
    "?response_type=code&client_id=123.apps.googleusercontent.com"
    "&code_challenge=Zx9_abc-DEF&code_challenge_method=S256"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A48103%2Fcallback"
    "&state=st4te&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.readonly"
)


def _query(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


class TestHelper:
    def test_google_url_gets_both_params(self):
        out = with_google_offline_access(GOOGLE_URL)
        q = dict(_query(out))
        assert q["access_type"] == "offline"
        assert q["prompt"] == "consent"

    def test_existing_params_kept_byte_for_byte_and_in_order(self):
        out = with_google_offline_access(GOOGLE_URL)
        assert out.startswith(GOOGLE_URL + "&")
        assert out == GOOGLE_URL + "&access_type=offline&prompt=consent"

    def test_existing_values_are_overwritten_not_duplicated(self):
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            "?access_type=online&client_id=c&prompt=select_account&state=s"
        )
        out = with_google_offline_access(url)
        q = _query(out)
        assert [v for k, v in q if k == "access_type"] == ["offline"]
        assert [v for k, v in q if k == "prompt"] == ["consent"]
        assert ("client_id", "c") in q and ("state", "s") in q

    def test_encoded_key_is_still_overwritten(self):
        url = "https://accounts.google.com/o/oauth2/v2/auth?access%5Ftype=online&state=s"
        q = _query(with_google_offline_access(url))
        assert [v for k, v in q if k == "access_type"] == ["offline"]

    def test_fragment_kept(self):
        out = with_google_offline_access(GOOGLE_URL + "#frag")
        assert out.endswith("&access_type=offline&prompt=consent#frag")

    def test_url_without_query(self):
        out = with_google_offline_access("https://accounts.google.com/o/oauth2/v2/auth")
        assert out == (
            "https://accounts.google.com/o/oauth2/v2/auth?access_type=offline&prompt=consent"
        )

    @pytest.mark.parametrize(
        "url",
        [
            # Lookalike and sibling hosts.
            "https://accounts.google.com.evil.test/o/oauth2/v2/auth?state=s",
            "https://evil.test/accounts.google.com/o/oauth2/v2/auth?state=s",
            "https://xaccounts.google.com/o/oauth2/v2/auth?state=s",
            "https://google.com/o/oauth2/v2/auth?state=s",
            "https://user@accounts.google.com/o/oauth2/v2/auth?state=s",
            "https://accounts.google.com:8443/o/oauth2/v2/auth?state=s",
            # Wrong scheme.
            "http://accounts.google.com/o/oauth2/v2/auth?state=s",
            # Other paths on the same host.
            "https://accounts.google.com/o/oauth2/auth?state=s",
            "https://accounts.google.com/o/oauth2/v2/auth/extra?state=s",
            "https://accounts.google.com/o/oauth2/v2/token?state=s",
            # Other providers.
            "https://mcp.linear.app/authorize?response_type=code&client_id=abc&state=xyz",
            "https://github.com/login/oauth/authorize?client_id=a&state=b",
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?state=s",
            # Not URLs.
            "",
            "not a url",
            "javascript:alert(1)",
        ],
    )
    def test_other_urls_untouched(self, url):
        assert with_google_offline_access(url) == url

    def test_unparseable_url_untouched(self):
        url = "https://[accounts.google.com/o/oauth2/v2/auth?state=s"
        assert with_google_offline_access(url) == url


class TestIngestPoints:
    @pytest.mark.asyncio
    async def test_init_drain_rewrites_google_url(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"sessionId": "s1", "serverName": "google-drive", "oauthUrl": GOOGLE_URL},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={
                    "sessionId": "s1",
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize?state=x",
                },
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = {r["serverName"]: r["oauthUrl"] for r in client.pop_pending_oauth_requests()}
        assert pending["google-drive"] == with_google_offline_access(GOOGLE_URL)
        assert pending["google-drive"] != GOOGLE_URL
        assert pending["linear"] == "https://mcp.linear.app/authorize?state=x"

    @pytest.mark.asyncio
    async def test_mid_turn_event_rewrites_google_url(self):
        client = AcpClient()
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "google-drive", "oauthUrl": GOOGLE_URL},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", oauth_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = [ev async for ev in client.stream_events("test")]

        oauth = [ev for ev in events if ev.kind == EVENT_MCP_OAUTH_REQUEST]
        assert len(oauth) == 1
        assert oauth[0].oauth_url == with_google_offline_access(GOOGLE_URL)
        assert oauth[0].oauth_url != GOOGLE_URL

    @pytest.mark.parametrize("key", ["oauthUrl", "url"])
    def test_session_handle_rewrites_google_url(self, key):
        # A KAS consent link reaches this function as the oauth_request frame
        # ``_answer_open_external_url`` queues, with the URL under oauthUrl.
        handle = SimpleNamespace(_oauth_emitted_servers=set())
        msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "google-drive", key: GOOGLE_URL},
        )
        request = _accept_oauth_request(handle, msg)
        assert request == {
            "serverName": "google-drive",
            "oauthUrl": with_google_offline_access(GOOGLE_URL),
        }

    def test_session_handle_leaves_other_providers(self):
        handle = SimpleNamespace(_oauth_emitted_servers=set())
        url = "https://mcp.linear.app/authorize?response_type=code&state=xyz"
        msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
        )
        assert _accept_oauth_request(handle, msg) == {"serverName": "linear", "oauthUrl": url}
