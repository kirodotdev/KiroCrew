"""Safe, dependency-free web fetch/search helpers for native Kiro BYO tools.

The web tools deliberately live outside ``native_tools.py`` so filesystem/process
execution and network execution keep separate trust boundaries. Every outbound
URL is resolved and validated against SSRF-sensitive destinations before a
request is made, then the TCP connection is pinned to that validated address.
Redirects are re-resolved, re-validated, and re-pinned hop by hop.
"""
from __future__ import annotations

from . import __version__

import gzip
import html
import http.client
import io
import ipaddress
import os
import socket
import ssl
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser


class WebToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ResolvedEndpoint:
    """One validated network destination for one URL hop.

    ``host`` remains the original logical hostname for the HTTP Host header and
    TLS SNI/certificate verification. ``sockaddr`` is the already-resolved public
    address the socket must actually connect to. Keeping both is what closes the
    DNS check/use gap without breaking virtual hosting or HTTPS certificates.
    """

    parsed: urllib.parse.SplitResult
    host: str
    port: int
    family: int
    socktype: int
    proto: int
    sockaddr: tuple


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection whose TCP connect target is an already-validated address."""

    def __init__(self, endpoint: _ResolvedEndpoint, *, timeout: float) -> None:
        self._endpoint = endpoint
        super().__init__(endpoint.host, endpoint.port, timeout=timeout)

    def connect(self) -> None:
        self.sock = _connect_socket(
            self._endpoint,
            timeout=self.timeout,
            source_address=self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection pinned to an IP while preserving hostname SNI/verification."""

    def __init__(
        self,
        endpoint: _ResolvedEndpoint,
        *,
        timeout: float,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self._endpoint = endpoint
        super().__init__(
            endpoint.host,
            endpoint.port,
            timeout=timeout,
            context=context or ssl.create_default_context(),
        )

    def connect(self) -> None:
        raw = _connect_socket(
            self._endpoint,
            timeout=self.timeout,
            source_address=self.source_address,
        )
        try:
            # The TCP peer is the pinned IP, but SNI and certificate hostname
            # validation intentionally remain bound to the original hostname.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def _connect_socket(
    endpoint: _ResolvedEndpoint,
    *,
    timeout: float,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Connect directly to ``endpoint.sockaddr`` without resolving the hostname.

    Using ``socket.socket(...).connect(sockaddr)`` instead of
    ``socket.create_connection((hostname, port))`` is deliberate: the latter may
    call ``getaddrinfo`` again and re-introduce the DNS rebinding/TOCTOU window.
    """

    sock = socket.socket(endpoint.family, endpoint.socktype, endpoint.proto)
    try:
        sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect(endpoint.sockaddr)
        return sock
    except Exception:
        sock.close()
        raise


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif not self._skip_depth and lowered in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and lowered in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self.parts.append(text)
                self.parts.append(" ")

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


class _DDGResultParser(HTMLParser):
    """Parse DuckDuckGo's lightweight HTML result page without dependencies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._capture: str | None = None
        self._buffer: list[str] = []

    @staticmethod
    def _classes(attrs) -> set[str]:
        for key, value in attrs:
            if key == "class" and value:
                return set(str(value).split())
        return set()

    @staticmethod
    def _href(attrs) -> str:
        for key, value in attrs:
            if key == "href" and value:
                return str(value)
        return ""

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        classes = self._classes(attrs)
        if tag.lower() == "a" and "result__a" in classes:
            if self._current and self._current.get("title"):
                self.results.append(self._current)
            self._current = {"title": "", "url": self._href(attrs), "snippet": ""}
            self._capture = "title"
            self._buffer = []
        elif self._current is not None and "result__snippet" in classes:
            self._capture = "snippet"
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if self._capture == "title" and tag.lower() == "a":
            if self._current is not None:
                self._current["title"] = " ".join("".join(self._buffer).split())
            self._capture = None
            self._buffer = []
        elif self._capture == "snippet" and tag.lower() in {"a", "div", "span"}:
            if self._current is not None:
                self._current["snippet"] = " ".join("".join(self._buffer).split())
            self._capture = None
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def finish(self) -> list[dict[str, str]]:
        if self._current and self._current.get("title"):
            self.results.append(self._current)
            self._current = None
        return self.results


def _load_fake_ip_networks() -> tuple:
    """Build the transparent-proxy Fake-IP allow-list.

    Transparent TUN proxies (Clash/Mihomo, sing-box, ...) answer DNS for public
    hostnames with a *synthetic* address and map the pinned TCP connection back
    to the real host at the TUN layer. Those synthetic answers are not globally
    routable, so a naive SSRF check blocks them even though the logical
    destination is public.

    RFC 2544 reserves ``198.18.0.0/15`` (IPv4) and ``2001:2::/48`` (IPv6) as
    benchmarking space, and that is exactly what the common proxies hand out as
    Fake-IP. Both are included by default. ``KIROAPI_EXTRA_FAKE_IP_NETWORKS`` may
    add comma-separated CIDRs for proxies configured with a different pool (e.g.
    ``28.0.0.0/8``), so a user is never hard-blocked by a non-default Fake-IP
    range they cannot change here.
    """
    networks = [
        ipaddress.ip_network("198.18.0.0/15"),  # RFC 2544 IPv4 benchmark / Fake-IP
        ipaddress.ip_network("2001:2::/48"),     # RFC 2544 IPv6 benchmark / Fake-IP
    ]
    extra = os.environ.get("KIROAPI_EXTRA_FAKE_IP_NETWORKS", "")
    for token in extra.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            # Ignore malformed entries rather than crash the web tool at import.
            continue
    return tuple(networks)


class SafeWebClient:
    USER_AGENT = f"KiroBYO/{__version__} (+https://kiro.dev/)"
    RFC2544_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
    FAKE_IP_NETWORKS = _load_fake_ip_networks()
    DEFAULT_SEARCH_ENDPOINT = os.environ.get(
        "KIROAPI_SEARCH_ENDPOINT", "https://html.duckduckgo.com/html/"
    )

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 5,
        search_endpoint: str | None = None,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.max_redirects = int(max_redirects)
        self.search_endpoint = search_endpoint or self.DEFAULT_SEARCH_ENDPOINT

    @classmethod
    def _dns_address_kind(cls, address: str) -> str:
        """Classify one resolver answer for outbound web policy.

        The returned string is intentionally more specific than a boolean so a
        rejected tool call can explain *why* it was rejected without leaking the
        full URL path or request body into logs.  ``198.18.0.0/15`` is the one
        narrow DNS-only exception for transparent Fake-IP/TUN environments.
        """
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return "invalid"
        if ip.is_global:
            return "public"
        # Transparent-proxy Fake-IP space (IPv4 198.18/15 and IPv6 2001:2::/48,
        # plus any KIROAPI_EXTRA_FAKE_IP_NETWORKS). These are the one narrow
        # DNS-only exception for TUN/Fake-IP environments.
        if any(ip in net for net in cls.FAKE_IP_NETWORKS):
            return "proxy-fake-ip"
        if ip.is_loopback:
            return "loopback"
        if ip.is_link_local:
            return "link-local"
        if ip.is_multicast:
            return "multicast"
        if ip.is_unspecified:
            return "unspecified"
        if getattr(ip, "is_private", False):
            return "private"
        # Python's ipaddress module intentionally treats several shared/reserved
        # ranges as non-global without exposing a dedicated predicate. Keep them
        # blocked and label them as special-use for diagnostics.
        return "special-use"

    def _resolve_public_url(self, url: str) -> _ResolvedEndpoint:
        """Resolve one URL hop, reject non-public answers, and select one pinned IP."""
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise WebToolError(f"invalid URL: {exc}") from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise WebToolError("web tools only allow http:// or https:// URLs")
        if not parsed.hostname:
            raise WebToolError("URL must include a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise WebToolError("URLs containing embedded credentials are not allowed")
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise WebToolError(f"invalid URL port: {exc}") from exc
        if port < 1 or port > 65535:
            raise WebToolError("invalid URL port")

        host = parsed.hostname.rstrip(".")
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise WebToolError("local/private web destinations are blocked")

        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None

        if literal is not None:
            if not literal.is_global:
                raise WebToolError("local/private web destinations are blocked")
            if literal.version == 6:
                return _ResolvedEndpoint(
                    parsed=parsed,
                    host=host,
                    port=port,
                    family=socket.AF_INET6,
                    socktype=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                    sockaddr=(str(literal), port, 0, 0),
                )
            return _ResolvedEndpoint(
                parsed=parsed,
                host=host,
                port=port,
                family=socket.AF_INET,
                socktype=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
                sockaddr=(str(literal), port),
            )

        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise WebToolError(f"cannot resolve web host {host}: {exc}") from exc

        public_candidates: list[tuple[int, int, int, tuple]] = []
        fake_ip_candidates: list[tuple[int, int, int, tuple]] = []
        blocked_kinds: set[str] = set()
        seen: set[tuple[int, tuple]] = set()
        for family, socktype, proto, _canonname, sockaddr in infos:
            if not sockaddr:
                continue
            address = str(sockaddr[0])
            kind = self._dns_address_kind(address)
            key = (family, sockaddr)
            if key in seen:
                continue
            seen.add(key)
            item = (family, socktype or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, sockaddr)
            if kind == "public":
                public_candidates.append(item)
            elif kind == "proxy-fake-ip":
                fake_ip_candidates.append(item)
            else:
                blocked_kinds.add(kind)

        # With IP pinning, unused resolver answers cannot become the TCP peer. If
        # at least one genuine public address exists, connect only to that address
        # and ignore blocked siblings. This avoids false positives from split DNS
        # or Windows resolver stacks that return an additional local/special-use
        # answer while preserving the SSRF boundary at connect().
        if public_candidates:
            candidates = public_candidates
        elif fake_ip_candidates and not blocked_kinds:
            # Pure RFC2544 Fake-IP DNS is a supported transparent-proxy case. If
            # the same lookup also returned a genuinely blocked address, remain
            # fail-closed because the Fake-IP transport delegates final routing to
            # the TUN/proxy rather than to our directly validated public address.
            candidates = fake_ip_candidates
        else:
            if blocked_kinds:
                kinds = ", ".join(sorted(blocked_kinds))
                raise WebToolError(
                    f"web host {host} resolved only to blocked destinations ({kinds})"
                )
            raise WebToolError(f"cannot resolve web host {host}: no usable addresses")

        # Prefer IPv4 over IPv6 among equally-ranked candidates. Transparent
        # Fake-IP/TUN proxies map IPv4 synthetic addresses most reliably, and a
        # dual-stack host that handed back both an IPv4 and an IPv6 answer should
        # not fail just because the IPv6 answer happened to sort first.
        candidates.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)

        family, socktype, proto, sockaddr = candidates[0]
        return _ResolvedEndpoint(
            parsed=parsed,
            host=host,
            port=port,
            family=family,
            socktype=socktype,
            proto=proto,
            sockaddr=sockaddr,
        )

    def _assert_public_url(self, url: str) -> urllib.parse.SplitResult:
        """Compatibility preflight used by the native-tool dispatcher.

        Actual requests call :meth:`_resolve_public_url` again and use the returned
        pinned address. A preflight result is never trusted as the network target.
        """
        return self._resolve_public_url(url).parsed

    @staticmethod
    def _request_target(parsed: urllib.parse.SplitResult) -> str:
        path = parsed.path or "/"
        return urllib.parse.urlunsplit(("", "", path, parsed.query, ""))

    def _request_once(
        self,
        endpoint: _ResolvedEndpoint,
        *,
        method: str,
        data: bytes | None,
    ) -> tuple[int, str, bytes, str | None]:
        """Perform one request against the pinned endpoint.

        Ambient HTTP(S)_PROXY settings are intentionally not consumed here. A proxy
        would become the actual TCP peer and could perform its own DNS resolution,
        defeating local IP pinning unless it is an explicitly trusted egress proxy.
        """
        connection_cls = _PinnedHTTPSConnection if endpoint.parsed.scheme.lower() == "https" else _PinnedHTTPConnection
        conn = connection_cls(endpoint, timeout=self.timeout_seconds)
        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        }
        body = data if method == "POST" else None
        try:
            conn.request(method, self._request_target(endpoint.parsed), body=body, headers=headers)
            response = conn.getresponse()
            status = int(response.status)
            content_type = str(response.headers.get("Content-Type") or "application/octet-stream")
            location = response.headers.get("Location")

            if status in {301, 302, 303, 307, 308}:
                response.close()
                return status, content_type, b"", location

            if status < 200 or status >= 300:
                error_body = response.read(2048)
                detail_text = error_body.decode("utf-8", errors="replace").strip()
                detail = f": {detail_text[:500]}" if detail_text else ""
                raise WebToolError(
                    f"HTTP {status} while fetching {urllib.parse.urlunsplit(endpoint.parsed)}{detail}"
                )

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self.max_bytes:
                        raise WebToolError(
                            f"web response too large ({content_length} bytes > {self.max_bytes})"
                        )
                except ValueError:
                    pass

            raw = response.read(self.max_bytes + 1)
            if len(raw) > self.max_bytes:
                raise WebToolError(f"web response exceeded {self.max_bytes} bytes")
            if str(response.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as gz:
                        expanded = gz.read(self.max_bytes + 1)
                except OSError as exc:
                    raise WebToolError("invalid gzip web response") from exc
                if len(expanded) > self.max_bytes:
                    raise WebToolError(f"decompressed web response exceeded {self.max_bytes} bytes")
                raw = expanded
            return status, content_type, raw, location
        except WebToolError:
            raise
        except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            raise WebToolError(
                f"web request failed for {urllib.parse.urlunsplit(endpoint.parsed)}: {exc}"
            ) from exc
        finally:
            conn.close()

    def _request_bytes(self, url: str, *, method: str = "GET", data: bytes | None = None) -> tuple[str, str, bytes]:
        current = url
        for _hop in range(self.max_redirects + 1):
            endpoint = self._resolve_public_url(current)
            status, content_type, raw, location = self._request_once(
                endpoint,
                method=method,
                data=data,
            )
            if status in {301, 302, 303, 307, 308}:
                if not location:
                    raise WebToolError(f"HTTP {status} redirect missing Location")
                current = urllib.parse.urljoin(current, location)
                if status == 303:
                    method, data = "GET", None
                continue
            return current, content_type, raw
        raise WebToolError(f"too many redirects (>{self.max_redirects})")

    @staticmethod
    def _decode_body(raw: bytes, content_type: str) -> str:
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.lower() == "charset" and value:
                charset = value.strip('"\'')
                break
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _html_to_text(source: str) -> str:
        parser = _HTMLTextExtractor()
        parser.feed(source)
        parser.close()
        return parser.text()

    def fetch(self, url: str, *, max_chars: int = 30_000) -> str:
        final_url, content_type, raw = self._request_bytes(url)
        media_type = content_type.split(";", 1)[0].strip().lower()
        textual = (
            media_type.startswith("text/")
            or media_type in {"application/json", "application/xml", "application/xhtml+xml", "application/javascript"}
            or media_type.endswith("+json")
            or media_type.endswith("+xml")
        )
        if not textual:
            raise WebToolError(f"unsupported non-text web content type: {media_type or 'unknown'}")
        source = self._decode_body(raw, content_type)
        text = self._html_to_text(source) if "html" in media_type else source.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + f"\n[content truncated after {max_chars} characters]"
        return f"URL: {final_url}\nContent-Type: {content_type}\n\n{text or '(empty response)'}"

    @staticmethod
    def _unwrap_ddg_url(url: str) -> str:
        absolute = html.unescape(url)
        if absolute.startswith("//"):
            absolute = "https:" + absolute
        parsed = urllib.parse.urlsplit(absolute)
        query = urllib.parse.parse_qs(parsed.query)
        uddg = query.get("uddg")
        if uddg:
            return urllib.parse.unquote(uddg[0])
        return absolute

    def search(self, query: str, *, max_results: int = 8) -> str:
        params = urllib.parse.urlencode({"q": query})
        endpoint = self.search_endpoint
        separator = "&" if "?" in endpoint else "?"
        final_url, content_type, raw = self._request_bytes(endpoint + separator + params)
        source = self._decode_body(raw, content_type)
        parser = _DDGResultParser()
        parser.feed(source)
        parser.close()
        rows = parser.finish()
        cleaned: list[dict[str, str]] = []
        for row in rows:
            title = " ".join(row.get("title", "").split())
            url = self._unwrap_ddg_url(row.get("url", ""))
            snippet = " ".join(row.get("snippet", "").split())
            if not title or not url:
                continue
            try:
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {"http", "https"}:
                    continue
            except ValueError:
                continue
            cleaned.append({"title": title, "url": url, "snippet": snippet})
            if len(cleaned) >= max_results:
                break
        if not cleaned:
            raise WebToolError(
                f"search provider returned no recognizable results for {query!r} ({final_url})"
            )
        lines = [f"Search results for: {query}"]
        for index, row in enumerate(cleaned, 1):
            lines.append(f"\n{index}. {row['title']}\n   {row['url']}")
            if row["snippet"]:
                lines.append(f"   {row['snippet']}")
        return "\n".join(lines)
