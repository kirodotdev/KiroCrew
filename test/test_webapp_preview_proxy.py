"""Proxy selection for the CloudFront preview HEAD request."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import webapp_preview


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "bypass", "use_proxy"),
    [
        ("https://d123.cloudfront.net/app/", "elsewhere.example.com", True),
        ("https://d123.cloudfront.net/app/", "d123.cloudfront.net", False),
        ("https://localhost/app/", "elsewhere.example.com", False),
        ("https://www.cloudfront.net.example.com/app/", "elsewhere.example.com", False),
    ],
)
async def test_preview_uses_the_proxy_only_for_allowed_destinations(
    monkeypatch, url, bypass, use_proxy
):
    proxy = "http://proxy.example.com:3128"
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("no_proxy", bypass)
    monkeypatch.setattr(webapp_preview, "_FRAMABLE_CACHE", {})
    response = MagicMock(headers={})
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.head = MagicMock()
    session.head.return_value.__aenter__.return_value = response
    monkeypatch.setattr(webapp_preview.aiohttp, "ClientSession", lambda **kwargs: session)

    result = await webapp_preview._remote_framable(url)

    if url.startswith("https://d123.cloudfront.net/"):
        assert result is True
        session.head.assert_called_once_with(
            url, allow_redirects=False, proxy=proxy if use_proxy else None
        )
    else:
        assert result is False
        session.head.assert_not_called()
