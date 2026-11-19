"""Outbound picture upload for the Weixin channel (``weixin/media_upload.py``).

Offline only: the iLink and CDN endpoints are fakes, and every upload is read
back through the inbound decoder so a picture the recipient could not decrypt
fails here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import types
from typing import Any

import pytest

from kiro_crew.weixin.client import (
    EP_GET_UPLOAD_URL,
    EP_SEND_MESSAGE,
    ITEM_IMAGE,
    WeixinClient,
    WeixinSendError,
)
from kiro_crew.weixin.media import WeixinMediaError, decrypt_aes_ecb, media_ref, parse_aes_key
from kiro_crew.weixin.media_upload import (
    MAX_IMAGE_BYTES,
    UPLOAD_MEDIA_TYPE_IMAGE,
    build_upload_url,
    encrypt_aes_ecb,
    upload_image,
)

_PLAIN = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3


class _UploadClient:
    """Records the two upload calls ``upload_image`` makes."""

    def __init__(self, ticket: dict[str, Any] | None = None) -> None:
        self.ticket = {"upload_param": "up+/param"} if ticket is None else ticket
        self.ticket_requests: list[dict[str, Any]] = []
        self.uploads: list[tuple[str, bytes]] = []

    async def get_upload_url(self, **kwargs: Any) -> dict[str, Any]:
        self.ticket_requests.append(kwargs)
        return self.ticket

    async def upload_cdn_object(self, url: str, data: bytes) -> str:
        self.uploads.append((url, data))
        return "download-param"


class TestEncryption:
    def test_ciphertext_decrypts_back_through_the_inbound_decoder(self) -> None:
        key = bytes(range(16))
        ciphertext = encrypt_aes_ecb(_PLAIN, key)
        assert len(ciphertext) % 16 == 0 and len(ciphertext) > len(_PLAIN)
        assert decrypt_aes_ecb(ciphertext, key) == _PLAIN

    def test_a_wrong_size_key_is_refused(self) -> None:
        with pytest.raises(WeixinMediaError):
            encrypt_aes_ecb(_PLAIN, b"short")

    def test_upload_url_percent_encodes_both_values(self) -> None:
        url = build_upload_url("a+b/c=", "k&1", "https://cdn.example/c2c")
        assert url == (
            "https://cdn.example/c2c/upload?encrypted_query_param=a%2Bb%2Fc%3D&filekey=k%261"
        )


class TestUploadImage:
    def test_the_item_references_an_upload_the_recipient_can_decrypt(self) -> None:
        client = _UploadClient()
        item = asyncio.run(upload_image(client, to_user_id="peer", data=_PLAIN))  # type: ignore[arg-type]

        [request] = client.ticket_requests
        [(url, ciphertext)] = client.uploads
        assert request["to_user_id"] == "peer"
        assert request["media_type"] == UPLOAD_MEDIA_TYPE_IMAGE
        assert request["rawsize"] == len(_PLAIN)
        assert request["rawfilemd5"] == hashlib.md5(_PLAIN, usedforsecurity=False).hexdigest()
        assert request["filesize"] == len(ciphertext)
        assert "encrypted_query_param=up%2B%2Fparam" in url
        assert f"filekey={request['filekey']}" in url

        assert item["type"] == ITEM_IMAGE
        assert item["image_item"]["mid_size"] == len(ciphertext)
        param, aes_key = media_ref(item, "image_item")
        assert param == "download-param"
        # The key the item carries is the one the ticket announced.
        assert parse_aes_key(aes_key).hex() == request["aeskey"]
        assert decrypt_aes_ecb(ciphertext, parse_aes_key(aes_key)) == _PLAIN

    def test_each_upload_draws_its_own_key_and_filekey(self, monkeypatch) -> None:
        """Each upload asks for a fresh key and filekey; pinned so nothing is left to chance."""
        keys = iter([b"\x01" * 16, b"\x02" * 16])
        filekeys = iter(["f1", "f2"])
        monkeypatch.setattr(
            "kiro_crew.weixin.media_upload.secrets",
            types.SimpleNamespace(
                token_bytes=lambda n: next(keys), token_hex=lambda n: next(filekeys)
            ),
        )
        client = _UploadClient()
        items = [
            asyncio.run(upload_image(client, to_user_id="peer", data=_PLAIN))  # type: ignore[arg-type]
            for _ in range(2)
        ]
        first, second = client.ticket_requests
        assert (first["aeskey"], first["filekey"]) == ((b"\x01" * 16).hex(), "f1")
        assert (second["aeskey"], second["filekey"]) == ((b"\x02" * 16).hex(), "f2")
        for item, key in zip(items, (b"\x01" * 16, b"\x02" * 16)):
            assert parse_aes_key(media_ref(item, "image_item")[1]) == key

    def test_a_ticket_without_upload_param_fails_before_any_upload(self) -> None:
        client = _UploadClient(ticket={"ret": 0})
        with pytest.raises(WeixinMediaError):
            asyncio.run(upload_image(client, to_user_id="peer", data=_PLAIN))  # type: ignore[arg-type]
        assert client.uploads == []

    @pytest.mark.parametrize("size", [0, MAX_IMAGE_BYTES + 1], ids=["empty", "oversized"])
    def test_empty_or_oversized_bytes_are_refused_without_a_request(self, size: int) -> None:
        data = b"x" * size
        client = _UploadClient()
        with pytest.raises(WeixinMediaError):
            asyncio.run(upload_image(client, to_user_id="peer", data=data))  # type: ignore[arg-type]
        assert client.ticket_requests == []


class TestClientEndpoints:
    def _client(self, resp: dict[str, Any]) -> tuple[WeixinClient, list[tuple[str, dict]]]:
        client = WeixinClient(token="t")
        posts: list[tuple[str, dict]] = []

        async def _post(endpoint: str, payload: dict, *, timeout_ms: int) -> dict:
            posts.append((endpoint, payload))
            return resp

        client._post = _post  # type: ignore[method-assign]
        return client, posts

    def test_send_image_puts_the_item_in_one_bot_message(self) -> None:
        client, posts = self._client({"ret": 0})
        item = {"type": ITEM_IMAGE, "image_item": {"media": {"encrypt_query_param": "p"}}}
        asyncio.run(
            client.send_image(to="peer", image_item=item, context_token="ctx", client_id="c1")
        )
        [(endpoint, payload)] = posts
        assert endpoint == EP_SEND_MESSAGE
        assert payload["msg"]["item_list"] == [item]
        assert payload["msg"]["to_user_id"] == "peer"
        assert payload["msg"]["context_token"] == "ctx"

    def test_a_rejected_image_send_raises(self) -> None:
        client, _ = self._client({"ret": -2})
        with pytest.raises(WeixinSendError):
            asyncio.run(
                client.send_image(to="peer", image_item={}, context_token=None, client_id="c1")
            )

    def test_get_upload_url_sends_the_ticket_fields_and_raises_on_refusal(self) -> None:
        client, posts = self._client({"upload_param": "u"})
        kwargs = dict(
            to_user_id="peer",
            filekey="fk",
            media_type=1,
            rawsize=3,
            rawfilemd5="m",
            filesize=16,
            aeskey="00" * 16,
        )
        assert asyncio.run(client.get_upload_url(**kwargs)) == {"upload_param": "u"}  # type: ignore[arg-type]
        [(endpoint, payload)] = posts
        assert endpoint == EP_GET_UPLOAD_URL
        assert payload == {**kwargs, "no_need_thumb": True}

        refused, _ = self._client({"errcode": -14})
        with pytest.raises(WeixinSendError):
            asyncio.run(refused.get_upload_url(**kwargs))  # type: ignore[arg-type]


class _CdnResponse:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self.status = status
        self.headers = headers

    async def text(self) -> str:
        return "body"

    async def __aenter__(self) -> "_CdnResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _CdnSession:
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        self._resp = _CdnResponse(status, headers)
        self.posts: list[tuple[str, bytes]] = []

    def post(self, url: str, *, data: bytes, headers: dict[str, str]) -> _CdnResponse:
        assert headers["Content-Type"] == "application/octet-stream"
        self.posts.append((url, data))
        return self._resp


class TestCdnUpload:
    def _upload(self, status: int, headers: dict[str, str]) -> tuple[WeixinClient, _CdnSession]:
        client = WeixinClient(token="t")
        session = _CdnSession(status, headers)
        client._session = session
        return client, session

    def test_returns_the_download_param_the_cdn_names(self) -> None:
        client, session = self._upload(200, {"x-encrypted-param": "dl"})
        assert asyncio.run(client.upload_cdn_object("https://cdn/upload", b"ct")) == "dl"
        assert session.posts == [("https://cdn/upload", b"ct")]

    @pytest.mark.parametrize(
        "status, headers",
        [(200, {}), (500, {"x-error-message": "boom"}), (400, {})],
    )
    def test_a_failed_or_unnamed_upload_raises(self, status: int, headers: dict[str, str]) -> None:
        client, _ = self._upload(status, headers)
        with pytest.raises(RuntimeError):
            asyncio.run(client.upload_cdn_object("https://cdn/upload", b"ct"))


def test_aes_key_encoding_is_base64_of_the_hex_key() -> None:
    """The file/voice encoding, which ``parse_aes_key`` already reads both ways."""
    item = asyncio.run(upload_image(_UploadClient(), to_user_id="p", data=_PLAIN))  # type: ignore[arg-type]
    raw = base64.b64decode(item["image_item"]["media"]["aes_key"])
    assert len(raw) == 32 and bytes.fromhex(raw.decode("ascii"))
