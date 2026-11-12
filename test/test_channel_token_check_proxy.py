"""A channel's dashboard token check reaches its service through the channel's proxy.

On a host whose only way out is ``HTTPS_PROXY`` the running channel connects through
the proxy, so a check that dials directly would report a good token as unverifiable.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

# The handlers facade binds the shared names (the verify timeouts) its owner modules read.
from kiro_crew.dashboard.handlers import messaging

#: Every variable the channels' proxy rule reads, cleared so the host's own cannot leak in.
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
_PROXY = "http://proxy.example:3128"


class _Resp:
    status = 200

    async def json(self, content_type: Any = None) -> dict[str, Any]:
        return {"ok": True}

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Session:
    def __init__(self, proxies: list[str | None]) -> None:
        self._proxies = proxies

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self._proxies.append(kwargs.get("proxy"))
        return _Resp()

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("check", "telegram_base", "expected"),
    [
        (messaging._validate_discord_token, None, _PROXY),
        (messaging._validate_telegram_token, None, _PROXY),
        (messaging._validate_webex_token, None, _PROXY),
        # A loopback Bot API base keeps the token on the machine, as the client does.
        (messaging._validate_telegram_token, "http://127.0.0.1:8081/bot{token}/{method}", None),
    ],
    ids=["discord", "telegram", "webex", "telegram-loopback-base"],
)
async def test_token_check_uses_the_channel_proxy(
    monkeypatch: pytest.MonkeyPatch,
    check: Any,
    telegram_base: str | None,
    expected: str | None,
) -> None:
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    if telegram_base is None:
        monkeypatch.delenv("TELEGRAM_API_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_API_BASE_URL", telegram_base)
    proxies: list[str | None] = []
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Session(proxies))

    assert await check("tok") is None
    assert proxies == [expected]
