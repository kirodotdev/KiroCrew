"""Ask Google for a refresh token on its MCP consent links.

Google returns a refresh token only when the consent link carries
``access_type=offline``, and re-issues one on a repeat consent only with
``prompt=consent``. Neither MCP engine Crew drives (KAS ``@kiro/agent`` and
kiro-cli v2) sends either parameter, so without this a Google MCP grant
expires after about an hour and the user signs in again.

Crew sees every consent URL before the browser does, so it sets the two
parameters itself, on one exact endpoint only. PKCE, ``state``,
``redirect_uri`` and every other parameter are kept byte for byte, so the
engine's code-for-token swap is unchanged. ``security/exfil_oauth.py``
already allows both parameter names.
"""

from __future__ import annotations

from urllib.parse import unquote_plus, urlsplit, urlunsplit

# The one endpoint rewritten. Matched exactly: scheme, the whole netloc (no
# port, no userinfo) and the whole path. Anything else is left alone.
_GOOGLE_SCHEME = "https"
_GOOGLE_NETLOC = "accounts.google.com"
_GOOGLE_PATH = "/o/oauth2/v2/auth"

# What is set, in this order. Any value already present is replaced.
_OFFLINE_PARAMS = (("access_type", "offline"), ("prompt", "consent"))
_OFFLINE_KEYS = frozenset(key for key, _ in _OFFLINE_PARAMS)


def with_google_offline_access(url: str) -> str:
    """Return *url* with ``access_type=offline`` and ``prompt=consent`` set.

    Only a URL whose scheme is ``https``, netloc exactly
    ``accounts.google.com`` and path exactly ``/o/oauth2/v2/auth`` changes.
    Every other URL, and any value that does not parse, is returned as is.
    Existing ``access_type`` and ``prompt`` values are dropped; every other
    query segment keeps its original bytes and order.
    """
    if not isinstance(url, str) or not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if (
        parts.scheme != _GOOGLE_SCHEME
        or parts.netloc != _GOOGLE_NETLOC
        or parts.path != _GOOGLE_PATH
    ):
        return url
    kept = [
        segment
        for segment in parts.query.split("&")
        if segment and unquote_plus(segment.split("=", 1)[0]) not in _OFFLINE_KEYS
    ]
    kept.extend(f"{key}={value}" for key, value in _OFFLINE_PARAMS)
    return urlunsplit(parts._replace(query="&".join(kept)))
