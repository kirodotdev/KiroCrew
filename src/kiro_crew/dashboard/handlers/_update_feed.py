"""Bounded release-feed transport with explicit proxy selection."""

import aiohttp

from kiro_crew.dashboard.handlers._shared import read_capped_response
from kiro_crew.http_proxy import resolve_proxy


async def fetch_feed_bytes(url: str, *, timeout_secs: float, max_bytes: int) -> tuple[int, bytes]:
    """GET the feed, returning its status and at most ``max_bytes + 1`` bytes."""
    timeout = aiohttp.ClientTimeout(total=timeout_secs)
    # Explicit proxy selection keeps aiohttp's netrc credential lookup disabled.
    proxy = resolve_proxy(url)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, proxy=proxy) as resp:
            # Drain chunked feeds to EOF; a single read can return a partial body.
            return resp.status, await read_capped_response(resp, max_bytes)
