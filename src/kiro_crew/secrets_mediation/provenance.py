"""Owner-authenticated provenance for the mediated-secret authorization policy.

The policy file (``secret-request-policy/policy.json``) names, per stored secret, the
exact https origin the agent may reach with it. Because a sandboxed agent runs
as the SAME uid as the owner, file ownership/mode cannot distinguish an
owner-authored policy from one an agent planted. The policy therefore carries
an owner-authored HMAC, and the host dispatcher refuses any authorization whose
signature does not verify.

The MAC key is DERIVED, never stored: an HMAC of a purpose label under the dashboard
token-signing secret (``token_signing.key``), the same file-less shape as
``mirror_admission._purpose_key``. That secret is independently hidden from every
sandbox tier, so no agent process can compute the key, and the purpose label keeps
it distinct from every other key derived from the same secret. Rotating
``token_signing.key`` changes the key, so every existing signature then fails closed
and the owner re-runs ``kirocrew secrets authorize``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped if the canonical serialization or MAC construction ever changes, so an
#: old signature cannot be replayed against a new verifier that reads it
#: differently. Part of the signed payload.
_PROVENANCE_VERSION = 1

#: The JSON key that carries the signature at the top level of the policy file.
SIGNATURE_FIELD = "sig"

#: The JSON key holding the map this module signs.
AUTHORIZATIONS_FIELD = "authorizations"

#: The wholly sandbox-hidden directory under the data home that holds the
#: owner-signed policy and its lock (``policy.policy_path``). One directory, so one
#: whole-directory mask covers every name in it, and a publication rename inside it
#: never replaces the dentry the mask is mounted on. Named publicly so the sandbox
#: and sensitive-path registries pin the exact leaf without importing this module.
REQUEST_POLICY_DIRNAME = "secret-request-policy"

#: Purpose label of the policy MAC key under the token-signing secret. Distinct from
#: every other label derived from that secret, so no other signature verifies here.
_POLICY_KEY_DOMAIN = b"kirocrew.secret_request_policy.sig.v3\x00"

#: The only shape this module ever writes: a lowercase hex SHA-256 HMAC. Anything
#: else (non-ASCII, wrong length) is refused before the constant-time compare,
#: which raises ``TypeError`` on non-ASCII text.
_SIGNATURE_RE = re.compile(r"[0-9a-f]{64}")


def _token_root() -> bytes:
    """Return the independently hidden gateway token-signing secret, persisted only.

    The policy is durable, so it is never signed with (or judged against) the
    loader's ephemeral fallback: a signature from a process-local key would be
    published, reported as success and then refused by every other process,
    with no repair once the key file is readable again. ``RuntimeError`` here
    makes signing refuse before anything is written and verification fail closed.
    """
    # Imported locally: token_secret pulls in modules that import token_auth,
    # and provenance is a low-level signing module in the sensitive-path floor —
    # keeping the import local avoids widening its module-load dependency chain.
    from kiro_crew.dashboard.token_secret import get_persisted_secret

    return get_persisted_secret()


def _secret_policy_key() -> bytes | None:
    """The policy MAC key, or ``None`` when the token-signing secret cannot be read."""
    try:
        root = _token_root()
    except (OSError, RuntimeError, ValueError):
        logger.warning("the dashboard token-signing secret could not be read", exc_info=True)
        return None
    return hmac.new(root, _POLICY_KEY_DOMAIN, hashlib.sha256).digest()


def _canonical_payload(authorizations: Any) -> bytes:
    """Return deterministic versioned bytes for *authorizations*."""
    body = json.dumps(
        authorizations,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{_PROVENANCE_VERSION}\n{body}".encode("utf-8")


def sign_secret_authorizations(authorizations: Any) -> str:
    """Return the owner HMAC, refusing when the token-signing secret is unavailable."""
    key = _secret_policy_key()
    if key is None:
        raise RuntimeError(
            "cannot read the persisted dashboard token-signing secret (token_signing.key) "
            "that signs the mediated-secret authorization policy; nothing was changed"
        )
    return hmac.new(key, _canonical_payload(authorizations), hashlib.sha256).hexdigest()


def verify_secret_authorizations(authorizations: Any, signature: Any) -> bool:
    """Whether *signature* is a valid owner HMAC over *authorizations*."""
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        return False
    key = _secret_policy_key()
    if key is None:
        return False
    expected = hmac.new(key, _canonical_payload(authorizations), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
