"""The ``browser`` tool's URL gate: public http(s), or a loopback origin the operator listed.

One module, two callers. The ``browser`` MCP tool (``mcp_tools/browser.py``, the
stdio shim) applies :func:`url_allowed` to the URL an op is ASKED to open, before
the op leaves the shim. The gateway (``browser_harness/ops.py``, through
``OpContext.url_allowed``) applies the same predicate to the URL the navigation
actually COMMITTED on, after every redirect: a public page that answers 302 to
``http://127.0.0.1:<port>/`` or a link-local address would otherwise be read back
through a browser that may be the person's logged-in Chrome, one hop past the
gate. DNS rebinding (a public name resolving to a private address) is the one
residual neither check can see without resolving names, and stays documented.

Imports nothing from the dashboard or ``mcp_core``, so the shim can import it
without pulling the gateway in; the gateway's own port is a parameter.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from kiro_crew.security import canonicalize_ip

_LOOPBACK_HOST_NAMES = {"localhost", "ip6-localhost", "ip6-loopback"}

# Code points a browser refuses in a host once it is percent-decoded.
_FORBIDDEN_DECODED_HOST_CHARS = frozenset("%/@?#[]:\\<>^| ")


def _percent_decoded_host(host: str) -> str | None:
    """Decode ``host`` once, as the browser's URL parser does.

    ``urlsplit`` keeps ``%31%32%37.0.0.1`` encoded, but the browser decodes it
    to ``127.0.0.1``. Returns None when the browser would reject the decoded
    host (invalid UTF-8, or a char a host may not carry).
    """
    try:
        decoded = unquote_to_bytes(host).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        ch in _FORBIDDEN_DECODED_HOST_CHARS or ord(ch) < 0x21 or ord(ch) == 0x7F for ch in decoded
    ):
        return None
    return decoded


def target_is_public(url: str) -> bool:
    """True iff ``url`` is an ordinary public http(s) page.

    The browser accepts ANY http(s) URL, so without this a ``navigate`` op could
    drive a loopback control plane (this dashboard included) or the
    ``169.254.169.254`` metadata endpoint. Kept LOCAL (not imported from the
    dashboard) so the ``kirocrew-core`` shim stays free of dashboard deps.

    ``is_global`` subsumes loopback/link-local/private/CGNAT/reserved in one
    predicate, applied to the address and any embedded IPv4 (``ipv4_mapped`` /
    ``sixtofour``). The host is first run through ``security.canonicalize_ip`` so
    an alternate IPv4 encoding the browser would still normalize -- decimal
    (``2852039166``), hex (``0x...``), octal, short/mixed, IPv6-mapped -- is
    resolved to dotted-quad and caught rather than mistaken for a DNS name. DNS
    names are NOT resolved (a blocking call + rebinding TOCTOU on the hot path);
    the residual public-name->private-IP risk is accepted and documented in the
    prompt.

    A percent-encoded host (``%31%32%37.0.0.1``, ``loc%61lhost``) is decoded
    first, as the browser does, so every check below sees the browser's host.
    """
    raw = url.strip()
    # Parser-differential SSRF: reject backslash (Chromium treats it as '/'),
    # and any control/whitespace char (tab/CR/LF and the rest), which browsers
    # strip or normalize but urlsplit does not -- e.g.
    # ``http://169.254.169.254\@example.com/`` parses to host ``example.com``
    # here yet Chromium navigates to the metadata endpoint. A legitimate URL
    # needs none of these (percent-encode instead), so refuse rather than parse.
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = parts.hostname or ""
    if "%" in host and ":" not in host:  # bracketed IPv6 keeps its own path
        decoded = _percent_decoded_host(host)
        if decoded is None:
            return False
        host = decoded
    host = host.lower().rstrip(".")
    if not host or host in _LOOPBACK_HOST_NAMES or host.endswith(".localhost"):
        return False
    # Reject non-ASCII hosts: a fullwidth-digit / IDN form the browser may
    # normalize toward a private IP would otherwise slip through the DNS branch
    # below. Legitimate IDN sites are reached by their ASCII punycode (xn--...).
    if any(ord(ch) > 0x7F for ch in host):
        return False
    try:
        addr: Any = ipaddress.ip_address(canonicalize_ip(host))
    except ValueError:
        return True  # a DNS name (not an IP in any encoding); public browsing unaffected
    candidates = [addr]
    for embedding in ("ipv4_mapped", "sixtofour"):
        embedded = getattr(addr, embedding, None)
        if embedded is not None:
            candidates.append(embedded)
    return all(c.is_global for c in candidates)


def target_is_allowed_local(url: str, allowed: frozenset[str], gateway_port: int | None) -> bool:
    """True iff ``url`` is a loopback origin the operator listed, exactly.

    The exception to :func:`target_is_public`, never a relaxation of
    it: the same parser-differential refusals run first, then the URL's own
    ``host:port`` (default port filled in from the scheme) must equal an entry
    byte for byte. Only the three spellings ``config.sections`` admits can ever
    match, so an alternate IPv4 encoding (``0x7f000001``, ``2130706433``) or a
    ``*.localhost`` name stays refused even when ``localhost:<port>`` is listed.
    Userinfo is refused outright so ``http://localhost:5173@host/`` cannot be
    read two ways. The gateway's own port (*gateway_port*) is refused even when
    listed: it is the control plane this gate exists to keep the browser away
    from; an unknown port refuses every local target.
    """
    if not allowed:
        return False
    raw = url.strip()
    if "\\" in raw or " " in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return False
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or "@" in parts.netloc:
        return False
    host = (parts.hostname or "").lower()
    if host == "::1":
        host = "[::1]"
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None:
        port = 443 if scheme == "https" else 80
    if f"{host}:{port}" not in allowed:
        return False
    if gateway_port is None:
        return False  # gateway port unknowable -> cannot prove this is not it
    return port != gateway_port


def url_allowed(url: Any, allowed_local: frozenset[str], gateway_port: int | None) -> bool:
    """The navigate gate: public, or an operator-listed loopback origin (never the gateway's)."""
    if not isinstance(url, str):
        return False
    return target_is_public(url) or target_is_allowed_local(url, allowed_local, gateway_port)


__all__ = ["target_is_allowed_local", "target_is_public", "url_allowed"]
