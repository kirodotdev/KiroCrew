"""Contract checks of the Feishu client against the REAL lark-oapi SDK.

``test_feishu_client`` fakes the SDK module by module. These tests instead run
the bot-identity probe and the inbound mention check through the installed
SDK's own client builder, request builder, token manager, transport and event
decoder, and stub only the HTTP call at the bottom of the SDK's transport. They
pin the parts a fake cannot prove: that the builder calls the probe makes exist
on the SDK, that the SDK turns them into an authenticated GET of the bot-info
endpoint with the probe's timeout, and that a receive event the SDK decodes
carries the mentioned bot's id where the mention check reads it.

The module skips when the optional ``feishu`` extra (lark-oapi) is not
installed.
"""

from __future__ import annotations

import importlib
import json
import types
from typing import Any

import pytest

lark = pytest.importorskip("lark_oapi")

from kiro_crew.feishu.client import CHAT_GROUP, LarkClient, LarkInbound  # noqa: E402

_TOKEN_URI = "/open-apis/auth/v3/tenant_access_token/internal"
_BOT_INFO_URI = "/open-apis/bot/v3/info"
_BOT_OPEN_ID = "ou_contract_bot"

# The bot-info envelope as Feishu documents it: the bot sits under "bot", not
# "data", alongside its display name and activation state.
_BOT_INFO_BODY = {
    "code": 0,
    "msg": "ok",
    "bot": {
        "activate_status": 2,
        "app_name": "Crew Bot",
        "avatar_url": "https://example.invalid/avatar.png",
        "open_id": _BOT_OPEN_ID,
    },
}


class _HttpResponse:
    """The slice of ``requests.Response`` the SDK transport reads."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self.content = json.dumps(body).encode("utf-8")


class _HttpStub:
    """Answers the SDK's HTTP calls by path and records every request."""

    def __init__(self, bot_info: dict[str, Any]) -> None:
        self.bot_info = bot_info
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _HttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if url.endswith(_TOKEN_URI):
            return _HttpResponse(
                {"code": 0, "msg": "ok", "tenant_access_token": "t-contract", "expire": 7200}
            )
        if url.endswith(_BOT_INFO_URI):
            return _HttpResponse(self.bot_info)
        raise AssertionError(f"unexpected SDK request: {method} {url}")


@pytest.fixture
def http_stub(monkeypatch: pytest.MonkeyPatch) -> _HttpStub:
    """Replace only the HTTP library inside the SDK's transport module."""
    stub = _HttpStub(dict(_BOT_INFO_BODY))
    transport = importlib.import_module("lark_oapi.core.http.transport")
    monkeypatch.setattr(transport, "requests", types.SimpleNamespace(request=stub.request))
    return stub


def _client(app_id: str) -> LarkClient:
    # A distinct app_id per test keeps the SDK's process-wide token cache from
    # answering one test with another test's token.
    return LarkClient(app_id=app_id, app_secret="contract-secret")


def test_probe_resolves_bot_open_id_through_the_real_sdk(http_stub: _HttpStub) -> None:
    client = _client("cli_contract_probe_ok")
    try:
        assert client._sync_fetch_bot_open_id() == _BOT_OPEN_ID
    finally:
        client._executor.shutdown(wait=True)

    token_call, info_call = http_stub.calls
    assert token_call["method"] == "POST"
    assert token_call["url"] == "https://open.feishu.cn" + _TOKEN_URI
    assert json.loads(token_call["data"]) == {
        "app_id": "cli_contract_probe_ok",
        "app_secret": "contract-secret",
    }
    assert info_call["method"] == "GET"
    assert info_call["url"] == "https://open.feishu.cn" + _BOT_INFO_URI
    assert info_call["headers"]["Authorization"] == "Bearer t-contract"
    assert info_call["timeout"] == 10.0


def test_probe_refuses_an_error_envelope_from_the_real_sdk(http_stub: _HttpStub) -> None:
    http_stub.bot_info = {"code": 99991672, "msg": "Access denied"}
    client = _client("cli_contract_probe_refused")
    try:
        with pytest.raises(ValueError, match="99991672"):
            client._sync_fetch_bot_open_id()
    finally:
        client._executor.shutdown(wait=True)


def _receive_frame(mentions: list[dict[str, Any]], text: str) -> bytes:
    """A schema 2.0 ``im.message.receive_v1`` frame for an allowed group."""
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": "ev_contract",
                "event_type": "im.message.receive_v1",
                "create_time": "1700000000000",
                "token": "",
                "app_id": "cli_contract_events",
                "tenant_key": "tk_contract",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "union_id": "on_sender",
                        "user_id": "u_sender",
                        "open_id": "ou_sender",
                    },
                    "sender_type": "user",
                    "tenant_key": "tk_contract",
                },
                "message": {
                    "message_id": "om_contract",
                    "create_time": "1700000000000",
                    "chat_id": "oc_contract_group",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": text}),
                    "mentions": mentions,
                },
            },
        }
    ).encode("utf-8")


def _mention(key: str, open_id: str, name: str) -> dict[str, Any]:
    return {
        "key": key,
        "id": {"union_id": "on_" + open_id, "user_id": "", "open_id": open_id},
        "name": name,
        "tenant_key": "tk_contract",
    }


def _decode(
    monkeypatch: pytest.MonkeyPatch, mentions: list[dict[str, Any]], text: str
) -> LarkInbound:
    """Run one frame through the SDK's own dispatcher into the client handler."""
    client = _client("cli_contract_events")
    client._bot_open_id = _BOT_OPEN_ID
    delivered: list[LarkInbound] = []
    monkeypatch.setattr(client, "_deliver", delivered.append)
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(client._handle_receive_v1)
        .build()
    )
    # The entry point the SDK's own WebSocket client hands a frame to: the
    # private name on SDKs that deprecate the public one, the public name on
    # older ones.
    dispatch = getattr(handler, "_do_without_validation", None) or handler.do_without_validation
    try:
        dispatch(_receive_frame(mentions, text))
    finally:
        client._executor.shutdown(wait=True)
    (inbound,) = delivered
    assert inbound.chat_type == CHAT_GROUP
    assert inbound.open_id == "ou_sender"
    return inbound


def test_sdk_decoded_bot_mention_marks_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    inbound = _decode(
        monkeypatch, [_mention("@_user_1", _BOT_OPEN_ID, "Crew Bot")], "@_user_1 hello"
    )
    assert inbound.bot_mentioned is True


def test_sdk_decoded_other_user_mention_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    inbound = _decode(
        monkeypatch, [_mention("@_user_1", "ou_someone_else", "Crew Bot")], "@_user_1 hello"
    )
    assert inbound.bot_mentioned is False


def test_sdk_decoded_message_without_mentions_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    inbound = _decode(monkeypatch, [], "hello")
    assert inbound.bot_mentioned is False
