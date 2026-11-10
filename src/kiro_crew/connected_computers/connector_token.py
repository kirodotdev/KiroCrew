"""The connector credential: scoped to one owner, one computer and one remote gateway.

Minted on the remote gateway at enrollment (over SSH, as its owner) with the
existing token primitive (:func:`kiro_crew.dashboard.token_auth_tokens.generate_token`:
HMAC-signed, expiring), never a new secret scheme. It is NOT a dashboard
credential: every dashboard acceptance path, logout included, refuses a token
whose ``aud`` is :data:`AUDIENCE` (``token_auth_tokens.validate_token``), so it
is accepted only by the connector route. Its own claims bind it:

* ``aud`` -- :data:`AUDIENCE`, so a dashboard token is refused here;
* ``gw`` -- this gateway's id, so a credential for one gateway is refused by another;
* ``device`` / ``fp`` -- the computer and its install fingerprint;
* ``cgen`` -- the device record's credential generation, so revoking, removing or
  re-enrolling the computer refuses every older credential at once;
* ``sub`` -- the owner who enrolled it, checked against the record.

Revocation lives on the device record only. Disconnect and a local Off's queued
forget mark the record revoked and bump ``cgen``; Remove marks it removed. The
dashboard's revocation generation and its logged-out-nonce list are never
consulted, so a dashboard logout does not end sharing. Every connect checks the
record first and expiry last (:func:`verify`), so a revoked credential is
refused as ``revoked`` / ``removed`` / ``superseded``, never as ``expired``. A
renewal over SSH presents the credential it replaces and
:func:`verify_for_renewal` checks everything except expiry: sharing renews for as
long as the record stays active at that generation. Loopback is never evidence:
the SSH forward delivers the connector from a loopback peer.

The bearer credential never travels on the socket. The connector sends its
unsigned payload and a nonce-bound proof of possession instead. The gateway
reconstructs the signature locally, checks the proof in constant time, then
runs the existing full credential verifier. Enrollment also hands the computer
a **peer key** (:func:`peer_key_from_secret`), derived from the gateway signing key and the
enrollment identities and generation. It authenticates the gateway's hello and
derives direction-separated session keys for every subsequent frame. Neither
the credential signature nor the peer key is disclosed to a socket relay.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Final

from kiro_crew.connected_computers.devices import DeviceRecord, DeviceRegistry

#: Also refused by every dashboard sink (``token_auth_tokens.CONNECTOR_AUDIENCE``).
AUDIENCE: Final[str] = "kirocrew-connected-computer"
#: The credential's life; the connector renews over SSH before (or after) it ends.
#: Equal to the token primitive's own cap (``MAX_SESSION_TTL_SECS``).
CREDENTIAL_TTL_SECS: Final[int] = 20 * 3600
#: Longest credential accepted, before any decoding.
MAX_CREDENTIAL_CHARS: Final[int] = 4096


class CredentialRejected(PermissionError):
    """A connector credential failed verification; ``code`` names the check."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or f"connector credential refused: {code}")
        self.code = code


def mint(record: DeviceRecord, gateway_id: str) -> tuple[str, float]:
    """``(credential, expires_at)`` for ``record`` on the gateway ``gateway_id``."""
    from kiro_crew.dashboard.token_auth_tokens import generate_token

    if not record.active:
        raise CredentialRejected("device_inactive", "this computer is not active here")
    if not gateway_id:
        raise CredentialRejected("gateway_unknown", "this gateway has no id")
    token = generate_token(
        record.owner,
        CREDENTIAL_TTL_SECS,
        register_nonce=False,
        extra={
            "aud": AUDIENCE,
            "gw": gateway_id,
            "device": record.device_id,
            "fp": record.fingerprint,
            "cgen": str(record.cgen),
        },
    )
    return token, time.time() + CREDENTIAL_TTL_SECS


_PEER_LABEL: Final[bytes] = b"kirocrew-connected-computer-peer-key-v1"
_PROOF_LABEL: Final[bytes] = b"kirocrew-connected-computer-gateway-proof-v1"
_AUTH_LABEL: Final[bytes] = b"kirocrew-connected-computer-client-proof-v1"


def _joined(*parts: str) -> bytes:
    return b"\0".join(p.encode("utf-8") for p in parts)


def peer_key_from_secret(record: DeviceRecord, gateway_id: str) -> str:
    """The peer key for ``record`` on ``gateway_id`` (64 hex), derived on demand."""
    from kiro_crew.dashboard.token_secret import _get_secret

    message = (
        _PEER_LABEL
        + b"\0"
        + _joined(gateway_id, record.device_id, record.fingerprint, str(record.cgen))
    )
    return hmac.new(_get_secret(), message, hashlib.sha256).hexdigest()


def gateway_proof(
    key: str, gateway_id: str, device_id: str, client_nonce: str, gateway_nonce: str
) -> str:
    """The gateway's proof that it holds ``key``, bound to both nonces (64 hex)."""
    message = _PROOF_LABEL + b"\0" + _joined(gateway_id, device_id, client_nonce, gateway_nonce)
    return hmac.new(bytes.fromhex(key), message, hashlib.sha256).hexdigest()


def proof_matches(key: str, expected_from: tuple[str, str, str, str], proof: str) -> bool:
    """Constant-time check of a ``hello`` proof; a malformed key never matches."""
    try:
        want = gateway_proof(key, *expected_from)
    except ValueError:
        return False
    return hmac.compare_digest(want, proof)


def connector_auth(token: str, bound: tuple[str, str, str, str]) -> dict[str, str]:
    """Prove possession without disclosing the bearer signature, bound to this socket."""
    payload, separator, signature = token.partition(".")
    if not separator or not payload or not signature or len(token) > MAX_CREDENTIAL_CHARS:
        raise CredentialRejected("malformed")
    proof = hmac.new(
        token.encode("utf-8"), _AUTH_LABEL + b"\0" + _joined(*bound), hashlib.sha256
    ).hexdigest()
    return {"op": "auth", "payload": payload, "proof": proof}


def verify_auth(
    payload: str,
    proof: str,
    bound: tuple[str, str, str, str],
    *,
    registry: DeviceRegistry,
) -> tuple[DeviceRecord, str]:
    """Reconstruct only locally; proof verification MUST precede credential admission.

    The unsigned payload alone conveys no authority. Only possession of its
    original signature can produce the expected client proof. Reusing the token
    primitive's signing seam preserves its wire format and all revocation checks.
    The returned token is private to the gateway's periodic recheck, never sent.
    """
    from kiro_crew.dashboard.token_auth_tokens import _b64url_decode, _sign

    if not payload or len(payload) > MAX_CREDENTIAL_CHARS or "." in payload:
        raise CredentialRejected("malformed")
    try:
        token = f"{payload}.{_sign(_b64url_decode(payload))}"
        expected = connector_auth(token, bound)["proof"]
    except (ValueError, UnicodeError) as exc:
        raise CredentialRejected("malformed") from exc
    if not hmac.compare_digest(expected, proof):
        raise CredentialRejected("invalid_proof")
    record = verify(token, gateway_id=bound[0], registry=registry)
    if record.device_id != bound[1]:
        raise CredentialRejected("wrong_device", "this proof belongs to another device")
    return record, token


def credential_generation(token: str) -> str:
    """The ``cgen`` claim of the computer's own credential, unverified ("" if none).

    The connector names it on the upgrade so the gateway proves itself under the
    peer key of THAT enrollment, even after a Disconnect moved the record on: the
    connector can then read the gateway's authenticated ``revoked`` refusal.
    It grants nothing: the credential is verified in full on the socket.
    """
    from kiro_crew.dashboard.token_auth_tokens import _b64url_decode

    try:
        data = json.loads(_b64url_decode(token.partition(".")[0]))
    except (ValueError, UnicodeError, TypeError):
        return ""
    value = data.get("cgen") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.isdigit() and len(value) <= 18 else ""


def _signed_payload(token: str) -> dict[str, Any]:
    """The payload of ``token`` when its signature checks; expiry is not looked at."""
    from kiro_crew.dashboard import token_auth_tokens as tokens

    if not isinstance(token, str) or not token or len(token) > MAX_CREDENTIAL_CHARS:
        raise CredentialRejected("malformed")
    payload, separator, signature = token.partition(".")
    try:
        raw = tokens._b64url_decode(payload)
        signed = bool(separator) and tokens._ct_eq(signature, tokens._sign(raw))
        data = json.loads(raw) if signed else None
    except (ValueError, UnicodeError, TypeError) as exc:
        raise CredentialRejected("invalid", "connector credential refused: invalid") from exc
    if not isinstance(data, dict):
        raise CredentialRejected("invalid", "connector credential refused: invalid signature")
    return data


def _claim(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _standing(data: dict[str, Any], gateway_id: str, registry: DeviceRegistry) -> DeviceRecord:
    """The active device a signed payload names, checking every claim but expiry.

    A record that is revoked or removed refuses with its status as the code,
    so the computer can tell an explicit Disconnect or Remove from anything else.
    """
    if data.get("kind") or data.get("app") or _claim(data, "aud") != AUDIENCE:
        # A dashboard session or app token is never a connector credential.
        raise CredentialRejected("wrong_audience")
    if not gateway_id or _claim(data, "gw") != gateway_id:
        raise CredentialRejected("wrong_gateway", "this credential is for another gateway")
    record = registry.get(_claim(data, "device"))
    if record is None:
        raise CredentialRejected("unknown_device", "this computer is not enrolled here")
    if _claim(data, "fp") != record.fingerprint:
        raise CredentialRejected("wrong_device", "this credential belongs to another computer")
    owner = _claim(data, "sub")
    if not owner or owner != record.owner:
        raise CredentialRejected("wrong_owner", "this credential was issued to another owner")
    if not record.active:
        raise CredentialRejected(record.status, f"this computer is {record.status} here")
    if _claim(data, "cgen") != str(record.cgen):
        raise CredentialRejected("superseded", "this credential was replaced or revoked")
    return record


def verify_standing(token: str, *, gateway_id: str, registry: DeviceRegistry) -> DeviceRecord:
    """The device ``token`` proves while its record stays active at its generation.

    Expiry is not checked: an admitted connection stays open past it, and only
    a revoke, a removal or a re-enrollment of the record ends it.
    """
    return _standing(_signed_payload(token), gateway_id, registry)


def verify_for_renewal(
    token: str, *, gateway_id: str, registry: DeviceRegistry, fingerprint: str, owner: str
) -> DeviceRecord:
    """The active device an old credential proves, expired or not.

    The SSH renewal presents the credential it replaces. Its signature,
    audience, gateway, device, fingerprint, owner and credential generation must
    all still hold, and the record must be active, so a credential from before a
    Disconnect, a Remove or a re-enrollment never renews. ``fingerprint`` and
    ``owner`` are the renewing computer and SSH user.
    """
    record = verify_standing(token, gateway_id=gateway_id, registry=registry)
    if record.fingerprint != fingerprint:
        raise CredentialRejected("wrong_device", "this credential belongs to another computer")
    if record.owner != owner:
        raise CredentialRejected("wrong_owner", "this credential was issued to another owner")
    return record


def verify(token: str, *, gateway_id: str, registry: DeviceRegistry) -> DeviceRecord:
    """The active device ``token`` proves, or raise :class:`CredentialRejected`.

    The record is checked before expiry, so an expired credential of a revoked
    or removed computer is refused as such, never as the renewable ``expired``.
    """
    data = _signed_payload(token)
    record = _standing(data, gateway_id, registry)
    expires = data.get("session_exp")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise CredentialRejected("invalid", "connector credential refused: no expiry")
    if time.time() > expires:
        raise CredentialRejected("expired", "connector credential refused: token expired")
    return record
