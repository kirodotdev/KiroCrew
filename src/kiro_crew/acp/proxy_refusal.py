"""Telling an HTTP proxy's own refusal apart from the service refusing a credential.

The status and "authentication required" wording that marks a rejected sign-in
also fires on a proxy's own 403/407, which then read as "not signed in" and point
at the sign-in card while the fix is the network's proxy. These helpers match per
LINE and only on a line that names the proxy (or the tunnel or CONNECT request it
runs), because a bare "status code 403" cannot be told apart from Kiro's own
rejection and must keep its sign-in verdict. A named service exception is never a
proxy's answer and is not affected.

Wording covered: reqwest/hyper's "407 Proxy Authentication Required" status
display and "tunnel error: ...", curl's "Received HTTP code 403 from proxy after
CONNECT" and "CONNECT tunnel failed", undici's "Proxy response (403) !== 200 when
HTTP Tunneling", and the "tunneling socket could not be established" of Node's
tunnel agents.

``kiro_crew.acp.transport_errors`` re-exports every name defined here.
"""

from __future__ import annotations

from collections.abc import Callable

_PROXY_WORDS = frozenset({"proxy", "tunnel", "tunneling"})
_REFUSAL_CODES = frozenset({"401", "403", "407"})
_REFUSAL_PHRASES = (
    "authentication required",
    "authorization required",
    "tunnel error",
    "tunnel failed",
    "tunneling failed",
    "could not be established",
    "!== 200",
)


def _split_on(line: str, keep: Callable[[str], bool]) -> list[str]:
    """*line* split into runs of the characters *keep* accepts."""
    return "".join(c if keep(c) else " " for c in line).split()


def _names_proxy(line: str) -> bool:
    if "CONNECT" in _split_on(line, str.isalpha):
        return True
    return not _PROXY_WORDS.isdisjoint(_split_on(line.lower(), str.isalpha))


def _refuses(line: str) -> bool:
    if not _REFUSAL_CODES.isdisjoint(_split_on(line, str.isalnum)):
        return True
    lower = line.lower()
    return any(phrase in lower for phrase in _REFUSAL_PHRASES)


#: The chat text for a request a proxy refused. Signing in again cannot help.
PROXY_REFUSAL_TEXT = (
    "A network proxy blocked the request to the model backend. Check the "
    "proxy settings on this computer (HTTPS_PROXY, NO_PROXY) or ask your "
    "network administrator to allow the Kiro service."
)


def _proxy_refusal_line(line: str) -> bool:
    return _names_proxy(line) and _refuses(line)


def _is_proxy_refusal(haystack: str) -> bool:
    """True when a line of *haystack* is an HTTP proxy refusing the request."""
    return any(_proxy_refusal_line(line) for line in haystack.splitlines())


def drop_proxy_refusal_lines(haystack: str) -> str:
    """*haystack* without the lines :func:`_is_proxy_refusal` claims."""
    return "\n".join(line for line in haystack.splitlines() if not _proxy_refusal_line(line))
