"""``LarkClient.send_image_reply``: upload the bytes, then reply with the image_key.

Apart from ``test_feishu_client.py``, which sits at the file-line cap. The SDK is
faked only where the call reaches it: the image-upload and reply request
builders, and the ``image`` and ``message`` resources.
"""

from __future__ import annotations

import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator

import pytest

from kiro_crew.feishu.client import LarkClient


class _Fields:
    """A request builder whose setters each record a field; ``build()`` returns them."""

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        def _set(value: Any) -> "_Fields":
            self.fields[name] = value
            return self

        return _set

    def build(self) -> dict[str, Any]:
        return dict(self.fields)


def _resp(ok: bool, **extra: Any) -> Any:
    return types.SimpleNamespace(success=lambda: ok, code=0 if ok else 99991672, msg="", **extra)


class _Image:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.ok = True

    def create(self, req: dict[str, Any]) -> Any:
        self.uploads.append(req)
        key = "img_v3_key" if self.ok else None
        return _resp(self.ok, data=types.SimpleNamespace(image_key=key))


class _Message:
    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []

    def reply(self, req: dict[str, Any]) -> Any:
        self.replies.append(req)
        return _resp(True)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[LarkClient]:
    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    for name in (
        "CreateImageRequest",
        "CreateImageRequestBody",
        "ReplyMessageRequest",
        "ReplyMessageRequestBody",
    ):
        setattr(im_v1, name, types.SimpleNamespace(builder=_Fields))
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", im_v1)
    # Built without __init__, which needs the real SDK to construct its clients.
    lark_client = object.__new__(LarkClient)
    lark_client._lark = types.SimpleNamespace(
        im=types.SimpleNamespace(v1=types.SimpleNamespace(image=_Image(), message=_Message()))
    )
    lark_client._executor = ThreadPoolExecutor(max_workers=1)
    yield lark_client
    lark_client._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_uploads_as_a_message_image_and_replies_with_its_key(client: LarkClient) -> None:
    assert await client.send_image_reply("msg-1", b"\x89PNG-bytes") is True

    (upload,) = client._lark.im.v1.image.uploads
    body = upload["request_body"]
    assert body["image_type"] == "message"
    assert body["image"].read() == b"\x89PNG-bytes"
    (reply,) = client._lark.im.v1.message.replies
    assert reply["message_id"] == "msg-1"
    assert reply["request_body"]["msg_type"] == "image"
    assert json.loads(reply["request_body"]["content"]) == {"image_key": "img_v3_key"}


@pytest.mark.asyncio
async def test_a_refused_upload_returns_false_and_sends_no_reply(client: LarkClient) -> None:
    client._lark.im.v1.image.ok = False
    assert await client.send_image_reply("msg-1", b"\x89PNG-bytes") is False
    assert client._lark.im.v1.message.replies == []
