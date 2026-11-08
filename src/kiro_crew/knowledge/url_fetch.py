"""The gateway's fetch of one web page for a Knowledge Library source.

Kiro Crew itself fetches a web-page source, over HTTPS, from a host the user
listed in ``knowledge.doc_ingest_hosts``. The list is empty by default, so a
default config fetches nothing. No agent is involved: the page is read here and
handed to the ingestion pipeline like an uploaded file.

Every request this module opens must pass two checks, for the first URL and
for every redirect hop:

* :func:`url_fetch_refusal` -- the user's rule: ``https://``, the default port,
  no user name or password, and a host equal to a listed one.
* :func:`kiro_crew.link_unfurl.vet_unfurl_url` -- the network rule: the host is
  resolved once and every address must be public, so a listed name that
  resolves to a loopback, private or link-local address is refused. The
  connection is then pinned to the addresses that check approved
  (:func:`~kiro_crew.link_unfurl.pinned_connector`), so a second DNS answer
  cannot move it (DNS rebinding).

A redirect is followed only to the same host, and is re-checked like the first
request. The whole fetch is bounded in time (:data:`FETCH_TIMEOUT_SECONDS`) and
in size (:data:`MAX_PAGE_BYTES`, enforced while reading, never by trusting
``Content-Length``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.helpers import parse_mimetype

from kiro_crew.link_unfurl import (
    MAX_REDIRECTS,
    UnfurlRejected,
    VettedUrl,
    base_content_type,
    decode_html,
    pinned_connector,
    vet_unfurl_url,
)

#: Total wall-clock budget for one page, every redirect hop included.
FETCH_TIMEOUT_SECONDS = 30.0
#: Hard cap on the page body. A page above it is refused, not truncated: half a
#: document stored as the whole one would answer searches with a wrong text.
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_URL_LENGTH = 2048

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT = "kirocrew-knowledge/1"
#: Content types stored, mapped to the suffix the file reader dispatches on.
_CONTENT_SUFFIXES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
}


class UrlFetchRefused(RuntimeError):
    """A URL the rules above do not let Kiro Crew fetch. ``code`` names the rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class UrlFetchFailed(RuntimeError):
    """An allowed fetch that did not produce a page: HTTP error, too large, wrong type."""


@dataclass(frozen=True)
class FetchedPage:
    """One fetched page, ready to be staged for ingestion."""

    url: str
    """The URL the body came from (after any same-host redirect)."""
    text: str
    """The body as text, undecodable bytes replaced: HTML by
    :func:`~kiro_crew.link_unfurl.decode_html` (the header's charset, then a
    ``<meta charset>``, then UTF-8); text and Markdown by the header's charset,
    else UTF-8."""
    suffix: str
    """The file suffix that selects the reader for this content type."""


def _decode_text(body: bytes, content_type: str) -> str:
    """Decode a text or Markdown body by its header's charset alone, else UTF-8.

    Never reads the body for a declaration: prose that quotes ``charset=`` must
    not change how the document is read.
    """
    charset = parse_mimetype(content_type).parameters.get("charset", "")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def url_fetch_refusal(url: object, allowed_hosts: Iterable[str]) -> tuple[str, str] | None:
    """Return ``(code, message)`` when Kiro Crew must not fetch *url*, else ``None``.

    The host must equal one entry of *allowed_hosts* exactly (case and a
    trailing dot aside); a subdomain of a listed host is a different host. The
    URL is also refused when its parsed host could differ from the host a
    reader sees: credentials before the host, a backslash, whitespace or a
    control character, a port other than 443, or a non-ASCII host. This rule
    reads no DNS; the address check is :func:`vet_unfurl_url`'s.
    """
    if not isinstance(url, str) or not url:
        return "url_invalid", "URL is required"
    if len(url) > MAX_URL_LENGTH:
        return "url_too_long", f"URL too long (max {MAX_URL_LENGTH})"
    if not url.startswith("https://"):
        return "url_not_https", "URI must start with https://"
    if "\\" in url or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return "url_invalid", "URL contains a character that is not allowed"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "url_invalid", "URL could not be parsed"
    if parts.username is not None or parts.password is not None:
        return "url_has_credentials", "URL must not carry a user name or password"
    if port not in (None, 443):
        return "url_port_not_allowed", "URL must use the default https port"
    host = _normalize_host(parts.hostname or "")
    if not host or not host.isascii():
        return "url_invalid", "URL has no valid host"
    allowed = {_normalize_host(h) for h in allowed_hosts if isinstance(h, str)}
    if host not in allowed:
        return (
            "url_host_not_allowed",
            f"{host} is not an allowed site. Add it to the allowed sites "
            "(knowledge.doc_ingest_hosts) to let Kiro Crew fetch pages from it.",
        )
    return None


def _host_of(url: str) -> str:
    return _normalize_host(urlsplit(url).hostname or "")


Resolver = Callable[[str, int], Sequence[str]]


async def _vet(url: str, resolve: Resolver | None) -> VettedUrl:
    """The network rule, off the loop: the vet performs a blocking ``getaddrinfo``."""
    try:
        return await asyncio.to_thread(vet_unfurl_url, url, resolve=resolve)
    except UnfurlRejected as exc:
        if exc.code == "invalid_url":
            raise UrlFetchRefused("url_invalid", "URL could not be parsed") from exc
        # ``blocked_url`` covers both an inward address and a name that did not
        # resolve: in either case no address was approved, so nothing is opened.
        raise UrlFetchRefused(
            "url_address_not_public",
            "The page's host did not resolve to a public address",
        ) from exc


async def _open(vetted: VettedUrl, timeout: aiohttp.ClientTimeout) -> tuple[int, Any, bytes]:
    """One request to *vetted*, pinned to its approved addresses; never follows a redirect.

    Returns the status, the headers and the body read under :data:`MAX_PAGE_BYTES`
    (empty for a redirect, whose body is never read).
    """
    async with aiohttp.ClientSession(
        connector=pinned_connector(vetted),
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
        auto_decompress=False,
    ) as session:
        async with session.get(vetted.url, allow_redirects=False) as resp:
            if resp.status in _REDIRECT_STATUSES:
                return resp.status, resp.headers, b""
            # Asked for identity and not decompressed, so the size cap bounds the
            # bytes held; a server that compresses anyway sent bytes that are not
            # the page's text.
            encoding = resp.headers.get("Content-Encoding", "").strip().lower()
            if encoding not in ("", "identity"):
                raise UrlFetchFailed(f"The page was sent compressed ({encoding})")
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_PAGE_BYTES:
                raise UrlFetchFailed("The page is larger than the size limit")
            body = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                if len(body) + len(chunk) > MAX_PAGE_BYTES:
                    raise UrlFetchFailed("The page is larger than the size limit")
                body.extend(chunk)
            return resp.status, resp.headers, bytes(body)


async def fetch_page(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    resolve: Resolver | None = None,
    opener: Callable[..., Any] | None = None,
) -> FetchedPage:
    """Fetch *url* under the module's rules and return the page.

    Raises :class:`UrlFetchRefused` when a rule refuses the first URL or a
    redirect hop, and :class:`UrlFetchFailed` when an allowed fetch does not
    yield a storable page. *resolve* and *opener* exist for tests, which must
    not touch DNS or a socket.
    """
    hosts = list(allowed_hosts)
    open_one = opener or _open
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    first_host = _host_of(url) if isinstance(url, str) else ""
    current = url
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            for _hop in range(MAX_REDIRECTS + 1):
                refusal = url_fetch_refusal(current, hosts)
                if refusal is not None:
                    raise UrlFetchRefused(*refusal)
                if _host_of(current) != first_host:
                    raise UrlFetchRefused(
                        "url_redirect_off_host", "The page redirected to a different host"
                    )
                vetted = await _vet(current, resolve)
                status, headers, body = await open_one(vetted, timeout)
                if status in _REDIRECT_STATUSES:
                    location = str(headers.get("Location", "")).strip()
                    if not location:
                        raise UrlFetchFailed("The page redirected without a location")
                    current = urljoin(vetted.url, location)
                    continue
                if status != 200:
                    raise UrlFetchFailed(f"The page answered HTTP {status}")
                content_type = str(headers.get("Content-Type", ""))
                media = base_content_type(content_type)
                suffix = _CONTENT_SUFFIXES.get(media)
                if suffix is None:
                    raise UrlFetchFailed(f"The page's content type {media or 'none'} is not text")
                # Only HTML declares its charset in its markup: text and Markdown
                # are decoded from the header alone, so an example declaration in
                # a document's prose cannot change how the document is read.
                if suffix == ".html":
                    text = decode_html(body, content_type)
                else:
                    text = _decode_text(body, content_type)
                if not text.strip():
                    raise UrlFetchFailed("The page is empty")
                return FetchedPage(url=vetted.url, text=text, suffix=suffix)
    except TimeoutError as exc:
        raise UrlFetchFailed("The page took too long to fetch") from exc
    except aiohttp.ClientError as exc:
        raise UrlFetchFailed(f"The page could not be fetched: {type(exc).__name__}") from exc
    raise UrlFetchFailed("The page redirected too many times")
