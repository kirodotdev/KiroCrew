"""Challenge-response proof that a loopback TCP peer is this install's gateway.

A sender that holds the gateway's internal secret but cannot resolve the peer of
a loopback TCP connection (the sandboxed Code Review Sage backend: inside the
Linux namespace sandbox it cannot read the gateway's ``/proc/<pid>/fd`` links)
asks the peer to prove it holds the same secret, on the very connection the
secret will travel over, before writing it:

1. The sender connects and sends ``GET /api/health`` carrying
   :data:`CHALLENGE_HEADER` with a fresh random nonce, and no credential.
2. The gateway answers with :data:`PROOF_HEADER`, an HMAC-SHA256 keyed with its
   internal secret over the nonce and the connection's two endpoints AS THE
   GATEWAY'S KERNEL REPORTS THEM (its own local address and the caller's).
3. The sender recomputes the HMAC with the secret it holds and its own view of
   the same connection (its peer address and its own local address). Only an
   exact match lets the secret-bearing request go out, on the same connection.

A listener that does not hold the secret cannot produce the HMAC. One that
forwards the challenge to the real gateway over a connection of its own gets an
HMAC over THAT connection's endpoints, which differ from the sender's, so the
relayed proof does not verify. The nonce is fresh per connection, so a proof
seen earlier cannot be replayed.

Both sides import this module, so the message layout has one definition.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets

#: Request header carrying the sender's nonce.
CHALLENGE_HEADER = "X-Gateway-Peer-Challenge"

#: Response header carrying the gateway's proof.
PROOF_HEADER = "X-Gateway-Peer-Proof"

#: Domain tag, so a proof is never a valid MAC for any other message keyed with
#: the same secret.
_DOMAIN = b"kirocrew-gateway-peer-proof/v1"

#: 32 random bytes, lowercase hex. Anything else is not a challenge.
_NONCE_RE = re.compile(r"[0-9a-f]{64}")


def new_nonce() -> str:
    """A fresh 256-bit nonce, lowercase hex."""
    return secrets.token_hex(32)


def is_valid_nonce(value: object) -> bool:
    """Whether *value* is a nonce in the exact form :func:`new_nonce` makes."""
    return isinstance(value, str) and _NONCE_RE.fullmatch(value) is not None


def endpoint_text(address: object) -> str | None:
    """Canonical ``"<ip> <port>"`` for a TCP socket address, or ``None``.

    Accepts the ``(host, port)`` and ``(host, port, flowinfo, scope_id)`` tuples
    ``getsockname`` / ``getpeername`` return. An IPv4-mapped IPv6 address (a
    dual-stack listener reporting an IPv4 caller) is reduced to its IPv4 form, so
    both ends describe the same connection with the same text. Anything that is
    not an IP address and a port (an ``AF_UNIX`` path, a malformed tuple) is
    ``None``.
    """
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host, port = address[0], address[1]
    if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool):
        return None
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not 0 < port < 65536:
        return None
    return f"{ip.compressed} {port}"


def peer_proof(
    secret: str, nonce: str, gateway_endpoint: object, client_endpoint: object
) -> str | None:
    """The proof for *nonce* on the connection between the two endpoints.

    *gateway_endpoint* is the gateway's own end of the connection and
    *client_endpoint* the caller's. ``None`` when there is no secret, the nonce
    is malformed, or either endpoint is not a TCP address, so neither side can
    compute a proof for a connection it cannot name.
    """
    gateway = endpoint_text(gateway_endpoint)
    client = endpoint_text(client_endpoint)
    if not secret or not is_valid_nonce(nonce) or gateway is None or client is None:
        return None
    message = b"\0".join((_DOMAIN, nonce.encode(), gateway.encode(), client.encode()))
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def proof_matches(expected: str | None, presented: object) -> bool:
    """Constant-time comparison; ``False`` when either side is absent."""
    if not expected or not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(expected.encode(), presented.encode())
