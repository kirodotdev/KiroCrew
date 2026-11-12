"""Which MCP sign-in links this gateway's own ACP children actually sent.

An ``mcp_oauth`` banner carries a consent URL the user clicks. A live banner is
born in exactly one place, :func:`~kiro_crew.dashboard.chat_turn.mcp_session._emit_mcp_oauth_request`,
from a URL the ACP child sent over the protocol. Every other copy of a banner is
read back from a transcript line, and a transcript line can be written by an
agent. The consent-URL gate (``security.oauth_url_contains_credential``)
deliberately leaves the protocol's own high-entropy parameters (``state``,
``nonce``, ``code_challenge``, ``client_id``) out of its markerless secret scan
at trusted endpoints, because a real consent URL is full of them. On a forged
row the same exemption would let a secret ride in one of those parameters and be
drawn as a real Authorize link.

So the exemption is only worth applying to a URL a child really sent. This
module records each one at the emit site, keyed by the child's process instance
and the exact URL, and the read-back gate
(:func:`~kiro_crew.dashboard.chat_utils_display._redact_meta_for_role`) shows a
link only when the row's ``child`` stamp and ``oauth_url`` are in the record.

The record lives in this process's memory and nowhere else, and its one writer
is the emit site. Nothing on disk is trusted, so a transcript line cannot add to
it. What it proves is that the URL arrived as an OAuth request on that child's
ACP stream; the integrity of that stream, and the URLs an MCP server the agent
configured makes the child send, are the emit-site gate's concern, not this
record's. Losing it on a gateway
restart costs nothing: a link is redeemable only while the child that minted it
is alive (it holds the loopback listener and the PKCE verifier), every child is
a subprocess of the gateway, and a respawned child gets a new process instance.
The read-time liveness gate already withdraws every such link after a restart,
so no sign-in that could still complete is lost.

Only SHA-256 digests are kept, so an entry is 32 bytes whatever the URL. The
store is bounded at :data:`CAP` entries; the least recently used goes first.
Losing an entry shows that banner as expired, the fail direction that removes a
link rather than inventing one.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

#: Most (child, URL) pairs remembered. Live entries are few (one open banner per
#: signed-out server per child); the cap bounds a long-running gateway's history,
#: and at about 100 bytes an entry it costs under half a megabyte.
CAP = 4096

_lock = threading.Lock()
_emitted: OrderedDict[bytes, None] = OrderedDict()


def _digest(child: str, url: str) -> bytes:
    # `surrogatepass`: a transcript row's JSON may decode to a lone surrogate,
    # which strict UTF-8 refuses to encode. That string can only miss the record,
    # and must not raise out of the render that reads it.
    raw = child.encode("utf-8", "surrogatepass") + b"\x00" + url.encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).digest()


def record_emitted(child: str, url: str) -> None:
    """Remember that the ACP child *child* sent the consent URL *url*.

    Does nothing for an empty child or URL: an unstamped banner is judged dead by
    the read gate anyway, and an empty URL has no link to vouch for.
    """
    if not child or not url:
        return
    key = _digest(child, url)
    with _lock:
        _emitted[key] = None
        _emitted.move_to_end(key)
        while len(_emitted) > CAP:
            _emitted.popitem(last=False)


def was_emitted(child: object, url: object) -> bool:
    """Whether the child stamped *child* sent exactly *url* to this gateway.

    Both arguments come from a transcript row, so anything that is not a
    non-empty string answers ``False``.
    """
    if not isinstance(child, str) or not isinstance(url, str) or not child or not url:
        return False
    key = _digest(child, url)
    with _lock:
        if key not in _emitted:
            return False
        _emitted.move_to_end(key)
        return True
