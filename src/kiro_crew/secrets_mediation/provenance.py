"""Owner-authenticated provenance for the mediated-secret authorization policy.

The policy file (``secret_request_policy.json``) names, per stored secret, the
exact https origin the agent may reach with it. Because a sandboxed agent runs
as the SAME uid as the owner, file ownership/mode cannot distinguish an
owner-authored policy from one an agent planted (including before the sandbox
mask applied on an upgrade). So the policy carries an owner-authored SIGNATURE,
and the host dispatcher refuses any authorization whose signature does not
verify — an agent-planted or tampered policy fails closed with no authorizations.

Key: the signature is an HMAC over the canonical authorizations map, keyed by
``memory_stores/.member-api-key`` — the same gateway-owned key the member-session
proofs use. That key sits in ``sandbox._CREW_HIDDEN_LEAVES`` (``memory_stores``),
so it is UNREADABLE from inside the agent sandbox: an agent cannot compute a
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
import os
import secrets as _secrets
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write, fsync_dir

#: Bumped if the canonical serialization or MAC construction ever changes, so an
#: old signature cannot be replayed against a new verifier that reads it
#: differently. Part of the signed payload.
_PROVENANCE_VERSION = 1

#: The JSON key that carries the signature at the top level of the policy file.
SIGNATURE_FIELD = "sig"

#: The JSON key holding the map this module signs.
AUTHORIZATIONS_FIELD = "authorizations"

#: Location of the gateway-owned signing key, relative to the config dir. Mirrors
#: ``member_memory_auth`` (``memory_stores/.member-api-key``), which
#: ``sandbox._CREW_HIDDEN_LEAVES`` masks from the agent sandbox.
_MEMBER_KEY_RELPATH = os.path.join("memory_stores", ".member-api-key")


def _member_key(config_dir: str | Path, *, create: bool) -> bytes | None:
    """The gateway-owned signing key under *config_dir*.

    Keyed off the SAME ``config_dir`` the loader is handed, so signing and
    verification always agree on which install's key to use. Returns ``None``
    when the key is absent/unreadable (verification then fails closed). When
    ``create`` and absent, mints a fresh 32-byte key with owner-only perms.
    """
    path = Path(config_dir).resolve() / _MEMBER_KEY_RELPATH
    if path.resolve() != path:
        return None
    try:
        existing = path.read_bytes()
        return existing if len(existing) == 32 else None
    except FileNotFoundError:
        if not create:
            return None
    except OSError:
        return None
    # create path: mint atomically. Reuse the proven staged + fsynced + hard-link
    # no-replace publication (the same pattern member_memory_auth uses for this
    # very key): atomic_write applies the owner-only ACL before writing any secret
    # bytes and fsyncs; the hard-link publish is atomic and no-replace, so an
    # interruption leaves the final name ABSENT rather than a corrupt short key,
    # and concurrent first creators converge on one inode.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    staged = path.with_name(f".{path.name}.{os.getpid()}.{_secrets.token_hex(8)}.tmp")
    try:
        atomic_write(staged, _secrets.token_bytes(32), fsync=True, restrict_to_owner=True)
        try:
            os.link(staged, path)
        except FileExistsError:
            pass  # another creator published first; read that winner below
        else:
            fsync_dir(path.parent, best_effort=True)
    except OSError:
        return None
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        minted = path.read_bytes()
        return minted if len(minted) == 32 else None
    except OSError:
        return None


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
    key = _member_key(config_dir, create=True)
    if key is None:
        raise RuntimeError(
            "cannot access the owner signing key (memory_stores/.member-api-key) "
            "to sign the mediated-secret authorization policy"
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
