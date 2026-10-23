"""CardKit verbs of ``LarkClient``: the streaming card plumbing.

The card calls go through the SDK's generic ``BaseRequest`` path, so these tests
install a minimal stand-in ``lark_oapi`` module that records each request and
answers with a scripted raw response. Nothing here touches the network.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest


class _Raw:
    def __init__(self, content: bytes | None, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code


class _Resp:
    def __init__(self, raw: _Raw) -> None:
        self.raw = raw


class _Sdk:
    """Shared script and request log for every fake SDK client."""

    script: list[Any] = []
    requests: list[dict[str, Any]] = []
    timeouts: list[Any] = []
    built = 0


class _ClientBuilder:
    def __init__(self) -> None:
        self._timeout: Any = None

    def app_id(self, _v: str) -> "_ClientBuilder":
        return self

    def app_secret(self, _v: str) -> "_ClientBuilder":
        return self

    def log_level(self, _v: Any) -> "_ClientBuilder":
        return self

    def timeout(self, v: Any) -> "_ClientBuilder":
        self._timeout = v
        return self

    def build(self) -> "_Client":
        _Sdk.built += 1
        return _Client(self._timeout)


class _Client:
    def __init__(self, timeout: Any = None) -> None:
        self.timeout = timeout

    @classmethod
    def builder(cls) -> _ClientBuilder:
        return _ClientBuilder()

    def request(self, req: dict[str, Any]) -> _Resp:
        _Sdk.requests.append(req)
        _Sdk.timeouts.append(self.timeout)
        entry = _Sdk.script.pop(0) if _Sdk.script else _Raw(b'{"code": 0}')
        if isinstance(entry, BaseException):
            raise entry
        return _Resp(entry)


class _RequestBuilder:
    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}

    def http_method(self, v: Any) -> "_RequestBuilder":
        self.fields["http_method"] = v
        return self

    def uri(self, v: str) -> "_RequestBuilder":
        self.fields["uri"] = v
        return self

    def token_types(self, v: Any) -> "_RequestBuilder":
        self.fields["token_types"] = v
        return self

    def body(self, v: Any) -> "_RequestBuilder":
        self.fields["body"] = v
        return self

    def build(self) -> dict[str, Any]:
        return dict(self.fields)


class _BaseRequest:
    @staticmethod
    def builder() -> _RequestBuilder:
        return _RequestBuilder()


class _HttpMethod:
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"


@pytest.fixture(autouse=True)
def _fake_lark(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("lark_oapi")
    mod.Client = _Client  # type: ignore[attr-defined]
    mod.LogLevel = types.SimpleNamespace(WARNING="WARNING")  # type: ignore[attr-defined]
    mod.BaseRequest = _BaseRequest  # type: ignore[attr-defined]
    mod.HttpMethod = _HttpMethod  # type: ignore[attr-defined]
    mod.AccessTokenType = types.SimpleNamespace(TENANT="TENANT")  # type: ignore[attr-defined]
    _floor_monkeypatch.setitem(sys.modules, "lark_oapi", mod)
    _Sdk.script = []
    _Sdk.requests = []
    _Sdk.timeouts = []
    _Sdk.built = 0


def _answer(*entries: Any) -> None:
    _Sdk.script = list(entries)


def _json(payload: dict[str, Any], status: int = 200) -> _Raw:
    return _Raw(json.dumps(payload).encode(), status)


def _client() -> Any:
    from kiro_crew.feishu.client import LarkClient

    return LarkClient(app_id="a", app_secret="s")


class TestSyncCardApi:
    def test_the_call_uses_the_sdk_with_a_tenant_token_and_a_timeout(self) -> None:
        from kiro_crew.feishu.client import _CARD_HTTP_TIMEOUT_S

        _answer(_json({"code": 0, "data": {"card_id": "c1"}}))
        client = _client()

        out = client._sync_card_api("POST", "/open-apis/cardkit/v1/cards", {"k": "v"})

        assert out == {"card_id": "c1"}
        req = _Sdk.requests[0]
        assert req["http_method"] == "POST"
        assert req["uri"] == "/open-apis/cardkit/v1/cards"
        assert req["token_types"] == {"TENANT"}
        assert req["body"] == {"k": "v"}
        assert _Sdk.timeouts == [_CARD_HTTP_TIMEOUT_S]

    def test_the_card_client_is_built_once(self) -> None:
        client = _client()
        built_by_init = _Sdk.built

        client._sync_card_api("PUT", "/open-apis/x", {})
        client._sync_card_api("PUT", "/open-apis/x", {})

        assert _Sdk.built == built_by_init + 1

    def test_no_body_is_attached_when_none_is_given(self) -> None:
        client = _client()
        client._sync_card_api("PATCH", "/open-apis/x", None)

        assert "body" not in _Sdk.requests[0]

    def test_http_200_with_a_nonzero_body_code_raises(self) -> None:
        """THE important one: Feishu reports business errors inside a 200 body.

        An exception-only reading would treat this as success for a call that did
        nothing, which is why the code is checked explicitly.
        """
        from kiro_crew.feishu.client import CardApiError

        _answer(_json({"code": 230020, "msg": "too fast"}))
        client = _client()

        with pytest.raises(CardApiError) as caught:
            client._sync_card_api("PUT", "/open-apis/x", None)

        assert caught.value.code == 230020
        assert caught.value.msg == "too fast"

    def test_an_http_error_carrying_a_body_code_uses_that_code(self) -> None:
        from kiro_crew.feishu.client import CardApiError

        _answer(_json({"code": 230011, "msg": "recalled"}, status=400))
        client = _client()

        with pytest.raises(CardApiError) as caught:
            client._sync_card_api("PUT", "/open-apis/x", None)

        assert caught.value.code == 230011

    def test_an_unparseable_error_body_falls_back_to_the_status(self) -> None:
        from kiro_crew.feishu.client import CardApiError

        _answer(_Raw(b"<html>gateway</html>", 503))
        client = _client()

        with pytest.raises(CardApiError) as caught:
            client._sync_card_api("GET", "/open-apis/x", None)

        assert caught.value.code == 503

    def test_an_error_status_without_a_body_code_raises(self) -> None:
        from kiro_crew.feishu.client import CardApiError

        _answer(_json({"msg": "bad gateway"}, status=502))
        client = _client()

        with pytest.raises(CardApiError) as caught:
            client._sync_card_api("GET", "/open-apis/x", None)

        assert caught.value.code == 502

    def test_a_missing_data_key_yields_an_empty_dict(self) -> None:
        _answer(_json({"code": 0}))
        client = _client()

        assert client._sync_card_api("PUT", "/open-apis/x", {}) == {}


class TestCardApi:
    @pytest.mark.asyncio
    async def test_card_api_returns_only_the_data_envelope(self) -> None:
        _answer(_json({"code": 0, "data": {"card_id": "c9"}}))
        client = _client()

        out = await client.card_api("POST", "/open-apis/cardkit/v1/cards", {"x": 1})

        assert out == {"card_id": "c9"}
        await client.close()

    @pytest.mark.asyncio
    async def test_send_card_reply_posts_an_interactive_message(self) -> None:
        """The message carries no text of its own -- only the card reference."""
        client = _client()

        await client.send_card_reply("om_123", "card_abc")

        reply = _Sdk.requests[-1]
        assert reply["uri"] == "/open-apis/im/v1/messages/om_123/reply"
        assert reply["body"]["msg_type"] == "interactive"
        assert json.loads(reply["body"]["content"]) == {
            "type": "card",
            "data": {"card_id": "card_abc"},
        }
        await client.close()
