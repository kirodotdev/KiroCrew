"""``LarkClient.send_new``: an unprompted send is ``message.create`` to an id.

Apart from ``test_feishu_client.py``, which sits at the file-line cap. The SDK is
faked only where the call reaches it: the two request builders and the
``message`` resource.
"""

from __future__ import annotations

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


class _Message:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.ok = True

    def create(self, req: dict[str, Any]) -> Any:
        self.created.append(req)
        ok = self.ok
        return types.SimpleNamespace(success=lambda: ok, code=0 if ok else 230053, msg="")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[LarkClient]:
    im_v1 = types.ModuleType("lark_oapi.api.im.v1")
    im_v1.CreateMessageRequest = types.SimpleNamespace(builder=_Fields)  # type: ignore[attr-defined]
    im_v1.CreateMessageRequestBody = types.SimpleNamespace(builder=_Fields)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", im_v1)
    # Built without __init__, which needs the real SDK to construct its clients.
    lark_client = object.__new__(LarkClient)
    lark_client._lark = types.SimpleNamespace(
        im=types.SimpleNamespace(v1=types.SimpleNamespace(message=_Message()))
    )
    lark_client._executor = ThreadPoolExecutor(max_workers=1)
    yield lark_client
    lark_client._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_send_new_creates_a_message_addressed_by_receive_id(client: LarkClient) -> None:
    message = client._lark.im.v1.message
    assert await client.send_new("open_id", "ou_peer", "你好") is True
    message.ok = False
    assert await client.send_new("chat_id", "oc_group", "refused") is False

    assert message.created == [
        {
            "receive_id_type": "open_id",
            "request_body": {
                "receive_id": "ou_peer",
                "msg_type": "text",
                "content": '{"text": "你好"}',
            },
        },
        {
            "receive_id_type": "chat_id",
            "request_body": {
                "receive_id": "oc_group",
                "msg_type": "text",
                "content": '{"text": "refused"}',
            },
        },
    ]
