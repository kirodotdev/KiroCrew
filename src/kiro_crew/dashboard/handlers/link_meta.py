"""``GET /api/link-meta`` — favicon + title for a link the model emitted.

Chat renders an http(s) link the assistant produced as a chip or card instead of
a bare URL; this endpoint is where the title, description and favicon come from.

Everything security-relevant about *which* URL may be fetched lives in
:mod:`kiro_crew.link_unfurl`. This module owns the parts that need a loop: the
fetch itself, the redirect walk (re-vetting every hop), the read caps, and the
cache/dedup/concurrency layer that keeps a chatty transcript from turning into a
fetch storm.

Two shapes worth knowing before editing:

* **The feature is opt-in and default OFF** (``cfg.dashboard.link_previews``).
  Enabling it means this machine fetches every link the model emits, so the
  disabled path returns 403 before parsing the URL and performs no network
  activity of any kind.
* **The transport is a seam** (:data:`_TRANSPORT`). The redirect walk, the
  content-type filter and the "``Content-Length`` is a hint, not a bound" read
  cap are all policy that must be testable, so they live above the seam and the
  tests substitute a fake transport rather than a fake server.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

if TYPE_CHECKING:  # `ResolveResult` only exists from aiohttp 3.10; see resolve() below.
    from aiohttp.abc import ResolveResult

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.link_unfurl import (
    FETCH_TIMEOUT_SECONDS,
    HTML_CONTENT_TYPES,
    MAX_BODY_BYTES,
    MAX_ICON_BYTES,
    MAX_REDIRECTS,
    UnfurlRejected,
    VettedUrl,
    base_content_type,
    build_icon_data_uri,
    decode_html,
    extract_meta,
    normalize_cache_key,
    vet_unfurl_url,
)

logger = logging.getLogger(__name__)

_ROUTE = "/api/link-meta"

#: A successful unfurl is cached for 6 h; a failure for 10 min. The asymmetry is
#: the point — a dead or blocked link must not be re-fetched on every render of
#: a streaming transcript, but it also must not be written off for a whole day
#: because the site was down for a minute.
_TTL_SECONDS = 6 * 60 * 60
_NEGATIVE_TTL_SECONDS = 10 * 60
_MAX_CACHE_ENTRIES = 500
#: In-flight upstream fetches. Bounded so one message full of links cannot open
#: hundreds of sockets from the user's machine at once.
_MAX_CONCURRENT_FETCHES = 4
#: Icon fetch attempts per page: the declared icon, then ``/favicon.ico``. Each
#: attempt is a separate vetted request, so this is a request budget, not a
#: parsing detail.
_MAX_ICON_ATTEMPTS = 2

_USER_AGENT = "KiroCrew-LinkPreview/1"
#: Redirect statuses followed. 300 (multiple choices) is excluded: it has no
#: single authoritative target.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass
class _CacheEntry:
    """``payload`` is the 200 body, or ``None`` for a cached failure."""

    payload: Optional[Dict[str, Any]]
    code: str
    status: int
    expires_at: float


# Module state. Single-process by design (the contract asks for an in-process
# dict): the gateway is one process, and a shared cache would need an eviction
# policy and an invalidation story for a decoration.
_CACHE: "OrderedDict[str, _CacheEntry]" = OrderedDict()
_INFLIGHT: Dict[str, "asyncio.Future[_CacheEntry]"] = {}
_SEMAPHORE: Optional[asyncio.Semaphore] = None
_SEMAPHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Concurrency gate bound to the running loop.

    Rebound per loop rather than created at import: a module-level
    ``asyncio.Semaphore()`` latches onto whichever loop constructed it, which
    breaks every test that runs its own ``asyncio.run`` (and the gateway's own
    restart path). Mirrors ``handlers.agents._get_config_lock``.
    """
    global _SEMAPHORE, _SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _SEMAPHORE is None or _SEMAPHORE_LOOP is not loop:
        _SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
        _SEMAPHORE_LOOP = loop
    return _SEMAPHORE


class _UnfurlFailed(Exception):
    """Upstream could not be turned into a preview. Surfaces as 502."""


# --- transport --------------------------------------------------------------


@dataclass
class _RawResponse:
    """One upstream response, body not yet read."""

    status: int
    headers: Mapping[str, str]
    chunks: AsyncIterator[bytes]


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolver that answers with the address the vet already approved.

    This is the mechanism that makes the vet meaningful. aiohttp would otherwise
    resolve the hostname itself when opening the connection — a second lookup,
    which an attacker-controlled DNS server is free to answer differently from
    the first (DNS rebinding). Pinning means the TCP connection goes to the exact
    address that was checked, while the hostname still drives SNI and the
    ``Host`` header so virtual hosting and certificate validation keep working.
    """

    def __init__(self, host: str, ip: str, port: int) -> None:
        self._host = host
        self._ip = ip
        self._port = port

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> "List[ResolveResult]":
        if host != self._host:
            # Cannot happen on the current call path (one session per vetted
            # URL), but a future caller reusing the session for a second host
            # would silently get the first host's address. Refuse instead.
            raise OSError(f"resolver pinned to {self._host}, refusing {host}")
        return [
            # Built as a plain dict, and `ResolveResult` imported only under
            # TYPE_CHECKING: that name landed in aiohttp 3.10, while setup.cfg
            # allows `aiohttp>=3.9`, so importing it at runtime would make this
            # handler — and therefore the whole gateway — fail to import on an
            # allowed install. 3.9 annotates `AbstractResolver.resolve` as
            # `List[Dict[str, Any]]` and every version since reads the same six
            # keys, so one dict satisfies both while the quoted annotation still
            # gives the type checker the real TypedDict.
            {
                "hostname": self._host,
                "host": self._ip,
                "port": port or self._port,
                "family": socket.AF_INET6 if ":" in self._ip else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self) -> None:
        return None


class _AiohttpTransport:
    """Real transport: one session per request, pinned to the vetted address."""

    async def get(self, vetted: VettedUrl) -> Tuple[_RawResponse, Any]:
        """Open *vetted* and return the response plus a closer handle.

        Returns the session alongside the response because the body is streamed:
        the caller reads chunks under its own cap and closes afterwards.
        Redirects are NOT followed here — the caller re-vets each hop, which it
        cannot do if aiohttp has already connected to the next one.
        """
        connector = aiohttp.TCPConnector(
            resolver=_PinnedResolver(vetted.wire_host, vetted.ip, vetted.port),
            limit=1,
            # The pinned resolver already returns a literal with its family, so
            # aiohttp must not narrow or re-derive it: constraining the family
            # here would either drop a valid IPv6 target or re-open the door to
            # a second lookup.
            family=socket.AF_UNSPEC,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS),
            headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
            auto_decompress=False,
        )
        try:
            resp = await session.get(vetted.url, allow_redirects=False)
        except Exception:
            await session.close()
            raise
        return (
            _RawResponse(
                status=resp.status,
                headers=dict(resp.headers),
                chunks=resp.content.iter_chunked(16 * 1024),
            ),
            session,
        )


#: Swapped wholesale in tests. Keep it a module global rather than a parameter:
#: the handler is reached through aiohttp's router, so there is nowhere to inject.
_TRANSPORT: Any = _AiohttpTransport()


async def _read_capped(raw: _RawResponse, limit: int) -> bytes:
    """Read at most *limit* bytes, raising :class:`_UnfurlFailed` if exceeded.

    ``Content-Length`` is used only as an early exit. It is a claim by the
    upstream, so a missing, wrong, or deliberately understated header must not
    let an unbounded body through — the accumulating check below is the actual
    bound, and it aborts mid-stream rather than after buffering the whole thing.
    """
    declared = raw.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _UnfurlFailed("declared body too large")
    buffered = bytearray()
    async for chunk in raw.chunks:
        buffered.extend(chunk)
        if len(buffered) > limit:
            raise _UnfurlFailed("body exceeded read cap")
    return bytes(buffered)


async def _fetch(vetted: VettedUrl, limit: int) -> Tuple[int, Mapping[str, str], bytes]:
    """One vetted request; returns status, headers and the capped body."""
    raw, closer = await _TRANSPORT.get(vetted)
    try:
        if raw.status in _REDIRECT_STATUSES:
            # Body is irrelevant on a redirect and may be large; skip reading it.
            return raw.status, raw.headers, b""
        return raw.status, raw.headers, await _read_capped(raw, limit)
    finally:
        if closer is not None:
            await closer.close()


async def _vet(url: str) -> VettedUrl:
    """Vet off-loop: the vet performs a blocking ``getaddrinfo``."""
    return await asyncio.to_thread(vet_unfurl_url, url)


async def _fetch_html(url: str) -> Tuple[VettedUrl, str]:
    """Walk up to :data:`MAX_REDIRECTS` hops and return the final HTML.

    Every hop is vetted before it is opened, including the ones the *upstream*
    chose. A vet applied only to the URL the client supplied is trivially
    bypassed: a public host that 302s to ``http://169.254.169.254/`` would be
    fetched on the attacker's behalf.
    """
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        vetted = await _vet(current)
        status, headers, body = await _fetch(vetted, MAX_BODY_BYTES)
        if status in _REDIRECT_STATUSES:
            location = headers.get("Location", "").strip()
            if not location:
                raise _UnfurlFailed("redirect without Location")
            current = urljoin(vetted.url, location)
            continue
        if status != 200:
            raise _UnfurlFailed(f"upstream status {status}")
        content_type = headers.get("Content-Type", "")
        if base_content_type(content_type) not in HTML_CONTENT_TYPES:
            # Not a page — a PDF or a video has no title to show, and parsing
            # arbitrary bytes as HTML invites nonsense titles.
            raise _UnfurlFailed("not html")
        return vetted, decode_html(body, content_type)
    raise _UnfurlFailed("too many redirects")


async def _fetch_icon(candidates: Tuple[str, ...]) -> str:
    """Try the icon candidates in order; return a data URI or ``""``.

    Every failure mode is non-fatal: a card with no favicon is complete, so a
    404, a wrong type, an oversized image or a blocked host all just mean "no
    icon" rather than failing the whole preview.
    """
    for candidate in candidates[:_MAX_ICON_ATTEMPTS]:
        try:
            vetted = await _vet(candidate)
            status, headers, body = await _fetch(vetted, MAX_ICON_BYTES)
        except (UnfurlRejected, _UnfurlFailed, aiohttp.ClientError, OSError, asyncio.TimeoutError):
            continue
        except Exception:  # noqa: BLE001 — an icon must never fail the preview
            logger.debug("link unfurl: icon fetch failed", exc_info=True)
            continue
        if status != 200:
            continue
        data_uri = build_icon_data_uri(headers.get("Content-Type", ""), body)
        if data_uri:
            return data_uri
    return ""


async def _build_payload(url: str) -> Dict[str, Any]:
    """Fetch and parse *url* into the 200 body. Raises on any refusal/failure."""
    vetted, html = await _fetch_html(url)
    meta = extract_meta(html, base_url=vetted.url)
    return {
        "url": vetted.url,
        "title": meta.title,
        "description": meta.description,
        "site_name": meta.site_name,
        "domain": vetted.domain,
        "icon": await _fetch_icon(meta.icon_candidates),
        "fetched_at": int(time.time()),
    }


# --- cache ------------------------------------------------------------------


def _cache_get(key: str) -> Optional[_CacheEntry]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if entry.expires_at <= time.time():
        _CACHE.pop(key, None)
        return None
    _CACHE.move_to_end(key)  # LRU-ish: a read is a use
    return entry


def _cache_put(key: str, entry: _CacheEntry) -> None:
    _CACHE[key] = entry
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_CACHE_ENTRIES:
        _CACHE.popitem(last=False)


def _has_userinfo(url: str) -> bool:
    """True when *url* carries userinfo, without raising on a malformed URL.

    Deliberately cheap and total: it runs before the cache key is derived, so it
    must not raise on input the vet would later reject anyway. A URL that will not
    parse has no userinfo to protect, so it falls through to the vet's own
    ``invalid_url``.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.username or parts.password)


def _response_for(entry: _CacheEntry) -> web.Response:
    if entry.payload is not None:
        return web.json_response(entry.payload)
    # Each status is a literal rather than a forwarded `status=entry.status`. A
    # computed status is unverifiable to the static error-code ratchet
    # (test_error_code_contract.py counts it as `dynamic_status`), and that
    # bucket is capped precisely so hoisting a status into a variable cannot be
    # used to slip an un-coded error past the gate.
    #
    # Only 400 and 502 reach here: a cached negative is either a rejected URL
    # or a failed fetch. `link_previews_disabled` (403) is answered before the
    # cache is consulted, because a disabled feature must not read — let alone
    # populate — a cache of fetched pages.
    if entry.status == 400:
        return web.json_response({"code": entry.code}, status=400)
    return web.json_response({"code": entry.code}, status=502)


async def _resolve_entry(key: str, url: str) -> _CacheEntry:
    """Produce (and cache) the entry for *url*, deduping concurrent callers.

    A paragraph can mention one link many times and several blocks can mention
    it at once, so the shared future — installed BEFORE the first await that
    could yield — is what turns N chips into one fetch. Without it the cache
    only helps callers that arrive after the first fetch has already finished,
    which in a streaming transcript is none of them.
    """
    pending = _INFLIGHT.get(key)
    if pending is not None:
        return await asyncio.shield(pending)

    loop = asyncio.get_running_loop()
    future: "asyncio.Future[_CacheEntry]" = loop.create_future()
    _INFLIGHT[key] = future
    try:
        async with _get_semaphore():
            try:
                payload = await _build_payload(url)
                entry = _CacheEntry(
                    payload=payload,
                    code="",
                    status=200,
                    expires_at=time.time() + _TTL_SECONDS,
                )
            except UnfurlRejected as exc:
                entry = _CacheEntry(
                    payload=None,
                    code=exc.code,
                    status=400,
                    expires_at=time.time() + _NEGATIVE_TTL_SECONDS,
                )
            except (_UnfurlFailed, aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                logger.debug("link unfurl failed for %s: %s", key[:120], exc)
                entry = _CacheEntry(
                    payload=None,
                    code="fetch_failed",
                    status=502,
                    expires_at=time.time() + _NEGATIVE_TTL_SECONDS,
                )
        _cache_put(key, entry)
        if not future.done():
            future.set_result(entry)
        return entry
    except BaseException as exc:
        # Cancellation included: a waiter shielded on this future would hang
        # forever if the owner unwound without resolving it.
        if not future.done():
            future.set_exception(exc)
        raise
    finally:
        _INFLIGHT.pop(key, None)


def reset_link_meta_cache() -> None:
    """Drop all cached state. Test hook — no production caller."""
    _CACHE.clear()
    _INFLIGHT.clear()


# --- handler ----------------------------------------------------------------


async def link_meta_get(request: web.Request) -> web.Response:
    """GET /api/link-meta?url=... — title/description/favicon for one link.

    Non-2xx bodies carry only a machine-readable ``code`` (no English prose):
    the dashboard ships in 10 languages and translates these client-side, per
    AGENTS.md on backend-owned strings.
    """
    # Offloaded: KiroCrewConfig.load() stats, reads and validates config files,
    # and this endpoint is hit once per distinct link in a transcript.
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if not cfg.dashboard.link_previews:
        # Checked FIRST and returned before the URL is even parsed. Enabling the
        # feature is consent to fetch arbitrary model-emitted links from this
        # machine; while it is off, nothing about the request may reach the
        # network — not a DNS lookup, not a cache-warming fetch.
        return web.json_response({"code": "link_previews_disabled"}, status=403)

    raw_url = request.query.get("url", "").strip()
    if not raw_url:
        return web.json_response({"code": "invalid_url"}, status=400)

    # Userinfo is refused HERE, before the cache key exists, and not only inside
    # the vet. The vet's rejection stops the credential being transmitted, but by
    # then this handler has already derived the cache key from the raw URL, so
    # `https://<key>:<secret>@host/` would be retained verbatim as an `_INFLIGHT`
    # and negative-cache key for the ten-minute failure TTL — a secret sitting in
    # process memory for no benefit. Stripping the userinfo from the key instead
    # would be worse: the entry would then collide with the credential-free URL
    # and poison a legitimate link's cache slot.
    if _has_userinfo(raw_url):
        return web.json_response({"code": "invalid_url"}, status=400)

    try:
        key = normalize_cache_key(raw_url)
    except ValueError:
        # `urlsplit` raises on a few malformed shapes rather than returning empty
        # parts — `http://[` (an unterminated IPv6 literal) is the reachable one.
        # The vet would refuse the URL anyway, but the cache key is derived BEFORE
        # the vet runs, so an unguarded call here surfaces as a 500 on a request
        # the endpoint should simply decline. Same coded 400 as any other
        # unusable URL.
        return web.json_response({"code": "invalid_url"}, status=400)
    cached = _cache_get(key)
    if cached is not None:
        return _response_for(cached)

    try:
        entry = await _resolve_entry(key, raw_url)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a decoration must not 500 the dashboard
        logger.warning("link unfurl: unexpected failure for %s", key[:120], exc_info=True)
        return web.json_response({"code": "fetch_failed"}, status=502)
    return _response_for(entry)


def setup_link_meta_routes(app: web.Application) -> None:
    """Register the link-preview route."""
    app.router.add_get(_ROUTE, link_meta_get)
