"""A rate-limited or server-failed ``getMe`` at startup is not a rejected token.

The gateway proves the token with ``getMe`` and treats ``TelegramAuthError`` as
fatal for that boot, while any other error starts polling anyway and retries. A
Bot API reply of ``ok: false`` with ``error_code`` 429 or a 5xx means Telegram
never judged the token, so it must take the retry path rather than leave the
channel down until the next restart.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.telegram.client import TelegramAuthError, TelegramClient


def _client_answering(body: dict) -> TelegramClient:
    client = TelegramClient(token="12345:testtoken")

    async def _call_raw(method: str, params: dict, timeout: int = 15) -> dict:
        return body

    client._call_raw = _call_raw  # type: ignore[method-assign]
    return client


@pytest.mark.parametrize(
    "body",
    [
        {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 5"},
        {"ok": False, "error_code": 500, "description": "Internal Server Error"},
        {"ok": False, "error_code": 502, "description": "Bad Gateway"},
    ],
    ids=["429", "500", "502"],
)
def test_a_transient_rejection_is_not_an_auth_error(body: dict) -> None:
    raised: BaseException | None = None
    try:
        asyncio.run(_client_answering(body).get_me())
    except Exception as exc:
        raised = exc
    assert isinstance(raised, ConnectionError), f"{raised!r} would keep the channel down"
    assert not isinstance(raised, TelegramAuthError)
    assert "testtoken" not in str(raised)


@pytest.mark.parametrize("code", [401, 404])
def test_a_real_rejection_is_still_an_auth_error(code: int) -> None:
    body = {"ok": False, "error_code": code, "description": "Unauthorized"}
    with pytest.raises(TelegramAuthError):
        asyncio.run(_client_answering(body).get_me())
