"""Pure, network-free logic behind the link-preview (unfurl) endpoint.

Two responsibilities, both deliberately kept out of the aiohttp handler so they
are unit-testable without a socket:

1. :func:`vet_unfurl_url` — the SSRF vet. It is the *only* place that decides a
   URL may be fetched, and it returns the resolved IP so the caller connects to
   that exact address. Resolving inside the vet and handing the address back is
   what closes the DNS-rebind window: if the caller re-resolved the hostname at
   connect time, an attacker-controlled DNS server could answer the vet with a
   public address and the connect with 127.0.0.1, and the vet would have proved
   nothing.
2. HTML/icon extraction — parsing an untrusted document into the handful of
   short, capped strings the client renders.

Nothing here opens a connection. The single OS call is ``getaddrinfo`` in the
vet, which is injectable (``resolve=``) precisely so tests never touch DNS.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from base64 import b64encode
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import yarl

from kiro_crew.security import canonicalize_ip

logger = logging.getLogger(__name__)

# --- limits (contract § 1) --------------------------------------------------

#: Total wall-clock budget for one upstream fetch.
FETCH_TIMEOUT_SECONDS = 5.0
#: Redirect hops followed; every hop is re-vetted, so this bounds the vet cost
#: as well as the fetch cost.
MAX_REDIRECTS = 3
#: Hard cap on an HTML body. Applied as a read cap, never by trusting
#: ``Content-Length``. A page above this is TRUNCATED to the cap, not dropped:
#: ``<head>`` — the only part a preview reads — is at the top, so the prefix is
#: everything. See ``link_meta._read_capped``.
MAX_BODY_BYTES = 256 * 1024
#: Hard cap on a favicon. An icon above this is DROPPED, not truncated: a
#: truncated image is a broken image, and the card reads fine without one.
MAX_ICON_BYTES = 32 * 1024

TITLE_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 300
#: Not in the contract, which leaves ``site_name`` uncapped. Bounded anyway:
#: every other field is, and an unbounded attacker-controlled string has no
#: business reaching the client.
SITE_NAME_MAX_CHARS = 200

ALLOWED_SCHEMES = frozenset({"http", "https"})
#: ``None`` covers a URL with no explicit port (the scheme default).
ALLOWED_PORTS = frozenset({80, 443})
#: Suffixes that never name a public host: Tor hidden services and mDNS names.
#: Both would otherwise sail past the IP checks — ``.local`` resolves through a
#: side channel and ``.onion`` does not resolve at all.
BLOCKED_HOST_SUFFIXES = (".onion", ".local")

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
#: ``image/svg+xml`` is deliberately absent: SVG is active content (script,
#: foreignObject, external refs) and this byte string lands in an ``<img src>``
#: as a ``data:`` URI. Raster only.
ICON_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)

_WHITESPACE_RUN = re.compile(r"\s+")
_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([\w.:+-]{1,40})""", re.IGNORECASE)
_COLOR_SCHEME_MEDIA = re.compile(r"prefers-color-scheme\s*:\s*(dark|light)", re.IGNORECASE)
_MEDIA_NEGATION = re.compile(r"\bnot\b", re.IGNORECASE)


class UnfurlRejected(Exception):
    """A URL must not be fetched. ``code`` is the wire code the client sees.

    Two codes, both surfaced as 400 by the handler:

    * ``invalid_url`` — the input is not an absolute http(s) URL at all
      (unparseable, wrong scheme, no host). A client bug or a user typo.
    * ``blocked_url`` — a well-formed URL that failed the SSRF vet. A
      *refusal*, not a malformed input.

    The split is only informational (both are 400 and the frontend renders a
    plain anchor either way), but conflating them would make an SSRF refusal
    indistinguishable from a typo in the logs.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VettedUrl:
    """A URL that passed the vet, plus the address the caller must connect to."""

    url: str
    """Normalized absolute URL (fragment stripped)."""
    scheme: str
    host: str
    """Hostname as written, lowercased, trailing dot stripped. Used for TLS SNI
    and the ``Host`` header — never for a second resolution."""
    wire_host: str
    """The host the HTTP client hands its resolver (``yarl.URL.raw_host``: IDNA
    encoded, trailing dot settled). Pin the resolver on THIS, not on ``host`` —
    they differ for every internationalized domain."""
    port: int
    """Effective port (the scheme default when the URL omitted one)."""
    ip: str
    """The resolved address to connect to. Equals ``host`` for an IP literal."""
    domain: str
    """Display host: lowercased, leading ``www.`` stripped."""


@dataclass(frozen=True)
class ExtractedMeta:
    """The capped strings pulled out of one HTML document."""

    title: str
    description: str
    site_name: str
    icon_candidates: Tuple[str, ...]
    """Absolute icon URLs for a light or unknown surface, best first,
    ``/favicon.ico`` last. Excludes icons the document scopes to a dark colour
    scheme — one of those on a light surface is the same invisible-glyph bug as
    using a light icon on a dark one."""
    dark_icon_candidates: Tuple[str, ...] = ()
    """Absolute icon URLs the document declares for
    ``(prefers-color-scheme: dark)``, best first. Empty for the great majority of
    sites, which ship one icon; no ``/favicon.ico`` fallback is appended, because
    a site's generic favicon is not a dark variant of anything."""
    title_complete: bool = False
    """Whether :attr:`title` is a whole, non-empty title rather than a fragment.

    True when a non-empty title exists AND either an ``og:title`` supplied it (a
    meta attribute parses atomically or not at all) or the ``<title>`` element
    that produced it closed. False for no title, an empty one, and a title whose
    element was left open — "nothing" and "half a word" are both incomplete. Only
    a caller holding a TRUNCATED document needs this; for a document read in full
    a short title is simply what the page served.
    """


def _reject_if_internal_ip(candidate: str) -> None:
    """Raise ``blocked_url`` if *candidate* parses as a non-public IP.

    Returns silently when *candidate* is not an IP literal at all — the caller
    then treats it as a hostname to resolve.

    Two normalizations matter here, and both are bypasses if skipped:

    * ``canonicalize_ip`` folds the alternate IPv4 encodings the OS resolver
      accepts but :mod:`ipaddress` rejects (``0x7f000001``, ``0177.0.0.1``,
      ``2130706433``, ``127.1``). Without it those fall through to the hostname
      branch and reach the metadata endpoint.
    * IPv4-mapped IPv6 is unwrapped explicitly. ``IPv6Address("::ffff:127.0.0.1")``
      reports ``is_loopback == False``, and its ``is_private`` only consults the
      mapped address on Python 3.13+ — so on every supported version below that,
      the mapped form is a clean bypass unless unwrapped by hand.
    """
    try:
        ip = ipaddress.ip_address(canonicalize_ip(candidate))
    except ValueError:
        return  # a hostname, not a literal
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # Allowlist AND denylist, deliberately both. `is_global` is the allowlist half
    # and is the only formulation that closes a whole class rather than one
    # instance — enumerating non-public categories already leaked
    # `100.64.0.0/10` (RFC 6598 shared space, what a Tailscale tailnet and most
    # CGNAT hand out), which CPython special-cases in `is_global` but not in
    # `is_private`.
    #
    # `is_global` alone is NOT sufficient either, because CPython's IPv6
    # `is_global` is just `not is_private` and its private table omits ranges that
    # are plainly not routable: `ff00::/8` multicast and the deprecated
    # `fec0::/10` site-local both report `is_global=True`. So every category flag
    # `ipaddress` exposes is also rejected. Neither half is redundant: the
    # allowlist catches what the flags forgot, the flags catch what `is_global`
    # forgot, and `test_vet_rejects_every_special_purpose_range` pins the union
    # against a table of IANA special-purpose prefixes so the next gap is found
    # by the suite instead of by a reviewer.
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_loopback
        or ip.is_unspecified
        # IPv6-only; absent on IPv4Address.
        or getattr(ip, "is_site_local", False)
    ):
        raise UnfurlRejected("blocked_url")


def _default_resolve(host: str, port: int) -> List[str]:
    """Resolve *host* to every address the OS would connect to."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def vet_unfurl_url(
    url: str,
    *,
    resolve: Optional[Callable[[str, int], Sequence[str]]] = None,
) -> VettedUrl:
    """Vet *url* for fetching and return the address to connect to.

    Raises :class:`UnfurlRejected`. Checks run in the contract's order so the
    cheapest rejections happen before the one that costs a DNS round trip.

    Blocking: performs one ``getaddrinfo``. Call it from a worker thread.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise UnfurlRejected("invalid_url") from None

    # 1. scheme
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnfurlRejected("invalid_url")

    # 2. host — absent means this is not an absolute URL; an internal IP literal
    #    means it is a refusal. Different codes, hence the split.
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnfurlRejected("invalid_url")
    # Userinfo is refused rather than stripped. aiohttp promotes URL userinfo into
    # an `Authorization: Basic` header unconditionally, so a model-emitted
    # `https://<key>:<secret>@host/` would ship the embedded secret to a
    # model-chosen host — and the same userinfo would then be written unredacted
    # to the gateway log by the debug/warning lines that echo the cache key.
    # Refusing is safe: the frontend's `safeHttpUrl` already declines this shape,
    # so no legitimate caller reaches here with credentials in the URL.
    if parts.username or parts.password:
        raise UnfurlRejected("invalid_url")
    _reject_if_internal_ip(host)

    # 4. port — before DNS: a rejected port makes the resolution wasted work.
    #    ``parts.port`` raises on a non-numeric or out-of-range port.
    try:
        explicit_port = parts.port
    except ValueError:
        raise UnfurlRejected("invalid_url") from None
    if explicit_port is not None and explicit_port not in ALLOWED_PORTS:
        raise UnfurlRejected("blocked_url")
    if explicit_port is not None:
        port = explicit_port
    else:
        # ALLOWED_SCHEMES is http/https only, so these two defaults cover it.
        port = 443 if scheme == "https" else 80

    # 5. suffixes that cannot name a public host
    for suffix in BLOCKED_HOST_SUFFIXES:
        if host.endswith(suffix):
            raise UnfurlRejected("blocked_url")

    # 3. DNS, exactly once. EVERY returned address is checked, not just the one
    #    we keep: a host that answers with both a public and a private address
    #    is a rebind attempt whichever one we happen to pick first.
    literal = canonicalize_ip(host)
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        resolver = resolve or _default_resolve
        try:
            addresses: Sequence[str] = resolver(host, port)
        except OSError:
            # NXDOMAIN, no route, resolver timeout. Fail CLOSED: an unresolvable
            # host is one whose addresses we could not check, and the vet's
            # contract is "safe to connect to", not "probably fine".
            raise UnfurlRejected("blocked_url") from None
        if not addresses:
            raise UnfurlRejected("blocked_url")
        for address in addresses:
            _reject_if_internal_ip(address)
    else:
        # An IP literal needs no resolution, but the CANONICAL form is what the
        # caller must dial — `host` may still be an alternate encoding
        # (`0x1a2b3c4d`) that no socket API accepts.
        addresses = [literal]

    normalized = urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))
    return VettedUrl(
        url=normalized,
        scheme=scheme,
        host=host,
        # The form the HTTP client will hand its resolver, which is NOT `host`:
        # aiohttp asks with `yarl.URL.raw_host`, i.e. the IDNA-encoded label set
        # (`例え.jp` -> `xn--r8jz45g.jp`) with the trailing dot already settled. A
        # resolver pinned on the unicode form would never match, so every IDN
        # link would be refused by our own pin, surface as a 502, and sit in the
        # negative cache for ten minutes. Deriving it here keeps "the pinned host
        # is exactly what the client asks for" true in one provable place.
        wire_host=yarl.URL(normalized).raw_host or host,
        port=port,
        ip=str(addresses[0]),
        domain=host[4:] if host.startswith("www.") else host,
    )


def normalize_cache_key(url: str) -> str:
    """Cache key for *url*: the fragment is dropped, everything else is kept.

    Deliberately conservative — a query string routinely selects the content, so
    stripping or reordering it would serve one page's title for another's URL.
    """
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))


def clean_text(value: str, cap: int) -> str:
    """Unescape entities, collapse whitespace runs, trim, and hard-cap length."""
    collapsed = _WHITESPACE_RUN.sub(" ", unescape(value)).strip()
    return collapsed[:cap]


def base_content_type(header: str) -> str:
    """Return the bare type from a ``Content-Type`` header, lowercased."""
    return header.split(";", 1)[0].strip().lower()


def decode_html(body: bytes, content_type: str) -> str:
    """Decode *body* to text, best-effort and never raising.

    Charset precedence: the ``Content-Type`` header, then a ``<meta charset>``
    in the first 4 KB, then UTF-8. ``errors="replace"`` throughout — a
    mis-declared encoding must degrade to a slightly mangled title, not a 502,
    because the encoding is attacker-controlled and this is a decoration.
    """
    candidates: List[str] = []
    if "charset=" in content_type.lower():
        candidates.append(content_type.lower().split("charset=", 1)[1].strip().strip("\"'; "))
    sniffed = _META_CHARSET.search(body[:4096])
    if sniffed is not None:
        candidates.append(sniffed.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")
    for charset in candidates:
        if not charset:
            continue
        try:
            return body.decode(charset, errors="replace")
        except (LookupError, ValueError):
            continue
    return body.decode("utf-8", errors="replace")


def icon_color_scheme(media: str) -> str:
    """``"dark"`` / ``"light"`` when *media* scopes an icon to one colour scheme.

    Returns ``""`` for no media attribute, a media query that says nothing about
    the colour scheme, and — deliberately — any query carrying ``not``. A
    negation inverts the match (``not all and (prefers-color-scheme: dark)``
    applies to LIGHT clients), and this is a single regex, not a media-query
    engine: guessing at a negation would put an icon in the wrong lane, which is
    exactly the failure being fixed. Treating it as unscoped keeps such an icon
    in the default lane, where it stays usable as the ordinary icon.
    """
    if not media or _MEDIA_NEGATION.search(media):
        return ""
    found = _COLOR_SCHEME_MEDIA.search(media)
    return found.group(1).lower() if found else ""


class _HeadParser(HTMLParser):
    """Collect the meta/title/icon tags from an untrusted document.

    Stops collecting at ``</head>`` (or the first ``<body>``): everything of
    interest is declared there, and a multi-hundred-KB body should not be walked
    for tags that cannot appear in it.

    ``convert_charrefs`` is left at its default (True) so entity decoding
    happens in the parser; :func:`clean_text` unescapes again, which is a no-op
    on already-decoded text and covers the attribute path.
    """

    def __init__(self) -> None:
        super().__init__()
        self.done = False
        self.og: Dict[str, str] = {}
        self.meta_name: Dict[str, str] = {}
        self.title = ""
        #: Whether a ``</title>`` was seen, i.e. whether :attr:`title` is the WHOLE
        #: element text rather than however much of it the document supplied. It
        #: matters only for a truncated document: a cut inside ``<title>`` leaves a
        #: word-fragment here that reads like a real title, and only the parser can
        #: tell the two apart.
        self.title_closed = False
        #: Every icon link in document order, as ``(href, colour scheme, is
        #: apple-touch)``. One list rather than a list per lane because document
        #: order is the preference order WITHIN a lane, and splitting on arrival
        #: would only have to be re-merged to preserve it.
        self._icon_links: List[Tuple[str, str, bool]] = []
        self._in_title = False

    # -- HTMLParser hooks ---------------------------------------------------

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self.done:
            return
        if tag == "body":
            self.done = True
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
            # A NEW title element is not yet complete, whatever an earlier one did.
            # `<title>First</title><title>Frag` must not inherit the first
            # element's closure, or a cut inside the second reads as whole.
            self.title_closed = False
        elif tag == "meta":
            prop = attr.get("property", "").strip().lower()
            content = attr.get("content", "")
            if prop.startswith("og:"):
                # First wins: a duplicated og:title is either a template bug or
                # an attempt to shadow the real one.
                self.og.setdefault(prop, content)
            name = attr.get("name", "").strip().lower()
            if name:
                self.meta_name.setdefault(name, content)
        elif tag == "link":
            rels = attr.get("rel", "").lower().split()
            href = attr.get("href", "").strip()
            if not href:
                return
            is_apple = any(r.startswith("apple-touch-icon") for r in rels)
            if "icon" in rels and not is_apple:
                self._icon_links.append((href, icon_color_scheme(attr.get("media", "")), False))
            elif is_apple:
                self._icon_links.append((href, icon_color_scheme(attr.get("media", "")), True))

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            # Only a close that matches an OPEN we saw proves the collected text is
            # whole. A stray `</title>` — before any `<title>`, or left over from
            # sloppy markup — must not mark a later, truncated title as complete.
            if self._in_title:
                self.title_closed = True
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.done and len(self.title) < TITLE_MAX_CHARS * 4:
            self.title += data

    # -- results ------------------------------------------------------------

    def ordered_icon_hrefs(self, *, dark: bool = False) -> List[str]:
        """One lane's hrefs, ``rel=icon`` first, then ``apple-touch-icon``.

        Plain ``rel=icon`` is preferred because it is the small one; an
        apple-touch-icon is often 180px+ and more likely to trip the 32 KB cap.

        ``dark=False`` is the default lane: every icon EXCEPT the ones the
        document scopes to a dark colour scheme. ``dark=True`` is only those.
        A light-scoped icon therefore sits in the default lane, which is correct
        — the default lane is what a light or unmeasurable surface uses.
        """
        keep = (lambda s: s == "dark") if dark else (lambda s: s != "dark")
        return [h for h, s, apple in self._icon_links if not apple and keep(s)] + [
            h for h, s, apple in self._icon_links if apple and keep(s)
        ]


def extract_meta(html: str, *, base_url: str) -> ExtractedMeta:
    """Pull title/description/site_name/icon candidates out of *html*.

    Never raises: a malformed document yields empty strings. ``HTMLParser`` is
    lenient by design, but a ``strict``-era ``AssertionError`` on pathological
    markup is still possible, so the walk is guarded — a broken page must not
    turn one chip into a 502.
    """
    parser = _HeadParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — untrusted input; any parse fault degrades
        logger.debug("link unfurl: HTML parse failed for %s", base_url[:120], exc_info=True)
    finally:
        try:
            parser.close()
        except Exception:  # noqa: BLE001
            pass

    title = clean_text(parser.og.get("og:title", "") or parser.title, TITLE_MAX_CHARS)
    description = clean_text(
        parser.og.get("og:description", "") or parser.meta_name.get("description", ""),
        DESCRIPTION_MAX_CHARS,
    )
    site_name = clean_text(parser.og.get("og:site_name", ""), SITE_NAME_MAX_CHARS)

    candidates: List[str] = []
    for href in parser.ordered_icon_hrefs():
        absolute = _absolutize(href, base_url)
        if absolute and absolute not in candidates:
            candidates.append(absolute)
    fallback = _absolutize("/favicon.ico", base_url)
    if fallback and fallback not in candidates:
        candidates.append(fallback)

    dark_candidates: List[str] = []
    for href in parser.ordered_icon_hrefs(dark=True):
        absolute = _absolutize(href, base_url)
        # An href listed in both lanes is not a dark VARIANT of anything, so it
        # would only buy a second fetch of bytes the default lane already has.
        if absolute and absolute not in dark_candidates and absolute not in candidates:
            dark_candidates.append(absolute)

    return ExtractedMeta(
        title=title,
        description=description,
        site_name=site_name,
        icon_candidates=tuple(candidates),
        dark_icon_candidates=tuple(dark_candidates),
        # "Complete" requires a title to exist AND to be whole. An `og:title` is
        # whole by construction (an attribute parses atomically or not at all);
        # otherwise the `<title>` element must have closed. The `bool(title)` term
        # is what keeps an empty-but-closed `<title></title>` from reading as a
        # complete title on a truncated document.
        title_complete=bool(title)
        and (bool(parser.og.get("og:title", "").strip()) or parser.title_closed),
    )


def _absolutize(href: str, base_url: str) -> str:
    """Resolve *href* against *base_url*, keeping only http(s) results.

    A ``data:`` icon href is dropped rather than passed through: it would skip
    the content-type allowlist and the size cap entirely, which is exactly the
    ``data:image/svg+xml`` hole the allowlist exists to close.
    """
    try:
        resolved = urljoin(base_url, unescape(href).strip())
    except ValueError:
        return ""
    return resolved if urlsplit(resolved).scheme in ALLOWED_SCHEMES else ""


def build_icon_data_uri(content_type: str, body: bytes) -> str:
    """Inline *body* as a ``data:`` URI, or return ``""`` to drop the icon.

    Inlining (rather than handing back the remote URL) is what keeps this
    endpoint from becoming an open fetch proxy: a second ``?url=``-driven asset
    route would let any page turn the gateway into a request laundry, and an
    ``<img src>`` on an attacker host is a tracking beacon.

    Dropped when the type is not in the raster allowlist (notably
    ``image/svg+xml``), the body is empty, or it exceeds
    :data:`MAX_ICON_BYTES` — oversized is DROPPED, never truncated, because
    half an image is a broken image.
    """
    kind = base_content_type(content_type)
    if kind not in ICON_CONTENT_TYPES:
        return ""
    if not body or len(body) > MAX_ICON_BYTES:
        return ""
    return f"data:{kind};base64," + b64encode(body).decode("ascii")
