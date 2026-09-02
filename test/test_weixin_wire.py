"""Wire-level tests for the Weixin (iLink) channel.

These run one level below the existing dispatcher tests: instead of replacing
``WeixinClient`` with a ``FakeClient``, they hand the REAL client a fake
``aiohttp`` session (:class:`FakeWireSession`) and assert on the bytes it would
put on the wire and how it parses what comes back. No credentials, no network,
no vendor account.

Why this layer earns its keep: the dispatcher tests are green whether or not
``send_message`` builds the payload iLink accepts, because they never let the
client construct one. The iLink QR bug (``qrcode_img_content`` is a scannable
URL served as ``text/html``, not image bytes) lived exactly here.

The response shapes below are PINNED FROM OBSERVED BEHAVIOUR, not invented --
notably ``_QR_START``, whose field values were captured from an
authorized live probe. A fixture is the only place a vendor shape should be
written down; every layer above is then verified against it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.testing.channel_fixtures import load_fixture
from kiro_crew.testing.fake_channel_wire import (
    FakeWireSession,
    UnroutedRequestError,
    WireResponse,
)
from kiro_crew.weixin.client import (
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    EP_GET_UPDATES,
    EP_SEND_MESSAGE,
    WeixinClient,
    WeixinSendError,
)

_BASE = "https://ilink.example.invalid"

# Shapes come from attributed fixture files, never inline literals: a fixture
# records WHERE the shape was observed (see kiro_crew.testing.channel_fixtures),
# so a live-probed contract is distinguishable from a guess and the live
# conformance lane can re-verify it against the vendor.
# The fixtures root lives in the TEST tree, so the layout coupling lives here
# too -- kiro_crew.testing.channel_fixtures ships in the wheel and deliberately
# has no default root (no test/ tree exists in an installed package).
CHANNEL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "channels"

_QR_START = load_fixture("weixin", "get_bot_qrcode", root=CHANNEL_FIXTURES).payload
_SEND_OK = load_fixture("weixin", "sendmessage_ok", root=CHANNEL_FIXTURES).payload


def _client(wire: FakeWireSession) -> WeixinClient:
    client = WeixinClient(token="bot-tok", base_url=_BASE, account_id="acct1")
    # The production seam: the client lazily creates its own session in
    # connect(). Pre-assigning means connect() is a no-op and no real socket is
    # ever opened, while every other line of the client runs untouched.
    client._session = wire
    return client


class TestOutboundPayload:
    """What the channel actually sends, built by the real client."""

    def test_send_message_payload_and_headers_match_the_ilink_contract(self) -> None:
        wire = FakeWireSession().route("POST", EP_SEND_MESSAGE, _SEND_OK)
        client = _client(wire)

        asyncio.run(
            client.send_message(
                to="user-1", text="hello", context_token="ctx-9", client_id="cid-1"
            )
        )

        req = wire.requests[0]
        assert req.method == "POST"
        # Envelope: every request carries base_info, and the message is nested
        # under "msg" -- shape the dispatcher tests never see.
        body = req.json_body
        assert "base_info" in body
        msg = body["msg"]
        assert msg["to_user_id"] == "user-1"
        assert msg["item_list"] == [{"type": 1, "text_item": {"text": "hello"}}]
        assert msg["context_token"] == "ctx-9"
        # Auth + framing headers are the client's job and are asserted here for
        # the first time.
        assert req.headers["Authorization"] == "Bearer bot-tok"
        assert req.headers["AuthorizationType"] == "ilink_bot_token"
        # Content-Length must count UTF-8 BYTES, not characters.
        assert req.headers["Content-Length"] == str(len(str(req.body).encode("utf-8")))

    def test_content_length_counts_utf8_bytes_for_multibyte_text(self) -> None:
        wire = FakeWireSession().route("POST", EP_SEND_MESSAGE, _SEND_OK)
        client = _client(wire)

        asyncio.run(
            client.send_message(
                to="u", text="已开始新对话", context_token=None, client_id="c"
            )
        )

        req = wire.requests[0]
        declared = int(req.headers["Content-Length"])
        assert declared == len(str(req.body).encode("utf-8"))
        assert declared > len(str(req.body))  # multibyte -> bytes exceed chars

    def test_context_token_is_omitted_when_absent(self) -> None:
        wire = FakeWireSession().route("POST", EP_SEND_MESSAGE, _SEND_OK)
        client = _client(wire)

        asyncio.run(
            client.send_message(to="u", text="hi", context_token=None, client_id="c")
        )

        assert "context_token" not in wire.requests[0].json_body["msg"]


class TestProtocolErrorParsing:
    """A 200 with a nonzero code means NOT delivered."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"errcode": -14},  # session expired
            {"ret": -2},  # rate limited (the other error key)
        ],
    )
    def test_nonzero_code_raises_instead_of_reporting_success(self, payload) -> None:
        wire = FakeWireSession().route("POST", EP_SEND_MESSAGE, payload)
        client = _client(wire)

        with pytest.raises(WeixinSendError):
            asyncio.run(
                client.send_message(
                    to="u", text="hi", context_token=None, client_id="c"
                )
            )

    def test_zero_code_is_success(self) -> None:
        wire = FakeWireSession().route("POST", EP_SEND_MESSAGE, {"errcode": 0, "ret": 0})
        client = _client(wire)

        resp = asyncio.run(
            client.send_message(to="u", text="hi", context_token=None, client_id="c")
        )
        assert resp["errcode"] == 0


class TestQrLoginShape:
    """Pins the live-probed QR contract that PR #711 fixed."""

    def test_qr_start_returns_a_scannable_url_not_image_bytes(self) -> None:
        wire = FakeWireSession().route(
            "GET",
            EP_GET_BOT_QR,
            WireResponse(body=_QR_START, content_type="text/html"),
        )
        client = _client(wire)

        out = asyncio.run(client.get_bot_qrcode())

        content = out["qrcode_img_content"]
        assert content.startswith("https://"), (
            "qrcode_img_content is a scannable LOGIN URL, not image bytes -- "
            "handing it straight to an <img src> is the #711 production bug"
        )
        assert not content.startswith("data:image"), "not a data URI at the API layer"

    def test_bot_type_is_carried_on_the_query_string(self) -> None:
        wire = FakeWireSession().route("GET", EP_GET_BOT_QR, _QR_START)
        client = _client(wire)

        asyncio.run(client.get_bot_qrcode(bot_type=3))

        assert "bot_type=3" in wire.requests[0].url

    def test_qrcode_is_url_encoded_in_the_status_poll(self) -> None:
        wire = FakeWireSession().route(
            "GET", EP_GET_QR_STATUS, {"status": "pending"}
        )
        client = _client(wire)

        asyncio.run(client.get_qrcode_status("a/b+c=d"))

        url = wire.requests[0].url
        assert "a%2Fb%2Bc%3Dd" in url, "unescaped qrcode would break the query"


class TestPollLoopSequencing:
    """A scripted response queue -- the poll loop sees a real sequence."""

    def test_get_updates_consumes_queued_responses_in_order(self) -> None:
        wire = FakeWireSession().route(
            "POST",
            EP_GET_UPDATES,
            [
                {"ret": 0, "msgs": [], "get_updates_buf": "buf-1"},
                {"ret": 0, "msgs": [{"text": "hi"}], "get_updates_buf": "buf-2"},
            ],
        )
        client = _client(wire)

        first = asyncio.run(client.get_updates("buf-0"))
        second = asyncio.run(client.get_updates(first["get_updates_buf"]))

        assert first["msgs"] == []
        assert second["msgs"] == [{"text": "hi"}]
        # The buffer cursor really round-trips through the request body.
        assert wire.requests[1].json_body["get_updates_buf"] == "buf-1"


class TestHarnessGuarantees:
    """The harness itself must fail closed."""

    def test_unrouted_endpoint_is_an_error_not_a_silent_default(self) -> None:
        wire = FakeWireSession()  # no routes
        client = _client(wire)

        with pytest.raises(UnroutedRequestError):
            asyncio.run(client.get_config(user_id="u", context_token=None))

    def test_http_error_status_surfaces_instead_of_being_parsed_as_json(self) -> None:
        wire = FakeWireSession().route(
            "POST", EP_SEND_MESSAGE, WireResponse(body="upstream boom", status=502)
        )
        client = _client(wire)

        with pytest.raises(RuntimeError, match="HTTP 502"):
            asyncio.run(
                client.send_message(
                    to="u", text="hi", context_token=None, client_id="c"
                )
            )
