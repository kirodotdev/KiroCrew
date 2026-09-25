"""Owner-authenticated provenance for the mediated-secret authorization policy.

The policy file (``secret_request_policy.json``) names, per stored secret, the
exact https origin the agent may reach with it. Because a sandboxed agent runs
as the SAME uid as the owner, file ownership/mode cannot distinguish an
owner-authored policy from one an agent planted (including before the sandbox
mask applied on an upgrade). So the policy carries an owner-authored SIGNATURE,
and the host dispatcher refuses any authorization whose signature does not
verify — an agent-planted or tampered policy fails closed with no authorizations.

Key: the signature is an HMAC over the canonical authorizations map, keyed by a
subkey DERIVED (domain-separated HMAC) from the SEL trust root ``sel_hmac.key``.
That root is created only by the gateway at boot and is deny-listed from every
agent shell and masked from the sandbox, so it is UNREADABLE from inside the
agent sandbox: an agent cannot compute a
valid signature for a policy it plants. The owner writes the policy host-side
(the ``kirocrew secrets authorize`` CLI), where the key is readable.

This is deliberately minimal and self-contained: it signs the exact bytes the
loader trusts (the ``authorizations`` object, canonically serialized), so adding,
removing, or editing any entry invalidates the signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

#: Bumped if the canonical serialization or MAC construction ever changes, so an
#: old signature cannot be replayed against a new verifier that reads it
#: differently. Part of the signed payload.
_PROVENANCE_VERSION = 1

#: The JSON key that carries the signature at the top level of the policy file.
SIGNATURE_FIELD = "sig"

#: The JSON key holding the map this module signs.
AUTHORIZATIONS_FIELD = "authorizations"

#: Location of the gateway-owned signing key, relative to the config dir. A
#: Domain-separation label for deriving the policy-signing key from the SEL trust
#: root. ``sel_hmac.key`` anchors several protocols; we NEVER sign with the raw
#: root, only a one-way HMAC subkey keyed by this label, so this protocol and the
#: SEL audit chain (and the pid-sidecar) never share a signing key. Bumping the
#: ``.vN`` suffix rotates this key without touching the root.
_SUBKEY_DOMAIN = b"kirocrew.secret_request_policy.sig.v1"


def _member_key(config_dir: str | Path, *, create: bool = False) -> bytes | None:
    """The policy-signing key, DERIVED from the SEL trust root.

    The signature is the sole discriminator between an owner-authored policy and
    an agent-planted one, so the key must be unforgeable from inside the sandbox.
    Rather than STORE a key file (which a pre-upgrade agent could preseed, before
    any sandbox mask exists, then have accepted as the owner key), the key is a
    one-way HMAC subkey of the SEL trust root (``sel_hmac.key``): created only by
    the gateway at boot, deny-listed from every agent shell, and never present on
    disk as this protocol's own file. An agent cannot read the root, so it cannot
    compute this subkey; there is no key file to plant.

    ``config_dir`` is accepted for signature compatibility but not used — the SEL
    root is resolved through ``sel_hmac_key_path`` so every process anchors on the
    one file. ``create`` is likewise vestigial: derivation needs no minting.
    Returns ``None`` when the SEL root is absent/short (verification fails closed).
    """
    from kiro_crew.sel import _sel_hmac_key_bytes, sel_hmac_key_path

    root = b""
    try:
        raw = sel_hmac_key_path().read_bytes()
        if len(raw) >= 32:
            root = raw
    except OSError:
        root = b""
    if not root:
        # The file did not load; fall back to the identical bytes the live SEL
        # validated at init, so a relocated/locked root does not permanently kill
        # this protocol (mirrors session_pid_sig).
        try:
            live = _sel_hmac_key_bytes()
        except Exception:  # noqa: BLE001 — no root, no key
            live = None
        if not live or len(live) < 32:
            return None
        root = live
    return hmac.new(root, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _canonical_payload(authorizations: Any) -> bytes:
    """Deterministic bytes for *authorizations*, independent of key order or
    whitespace, prefixed with the provenance version so a v1 MAC cannot be
    reinterpreted under a future scheme."""
    body = json.dumps(
        authorizations,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{_PROVENANCE_VERSION}\n{body}".encode("utf-8")


def sign_authorizations(authorizations: Any, config_dir: str | Path) -> str:
    """Return the hex HMAC for *authorizations*. Host-side (owner) use only.

    Raises ``RuntimeError`` if the signing key cannot be created/read — signing
    must never silently produce an unverifiable policy.
    """
    key = _member_key(config_dir)
    if key is None:
        raise RuntimeError(
            "cannot derive the owner signing key from the SEL trust root "
            "(sel_hmac.key) to sign the mediated-secret authorization policy"
        )
    return hmac.new(key, _canonical_payload(authorizations), hashlib.sha256).hexdigest()


def verify_authorizations(authorizations: Any, signature: Any, config_dir: str | Path) -> bool:
    """Whether *signature* is a valid owner HMAC over *authorizations*.

    Fails closed (returns ``False``) on a missing/short/typewrong signature, an
    unreadable key, or any mismatch. Constant-time comparison.
    """
    if not isinstance(signature, str) or not signature:
        return False
    key = _member_key(config_dir, create=False)
    if key is None:
        return False
    expected = hmac.new(key, _canonical_payload(authorizations), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
